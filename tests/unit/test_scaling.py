"""
Tests for VectrixDB auto-scaling functionality.

Covers vectrixdb/core/scaling.py: ScalingConfig (including from_env), the
metrics collector and resource monitor, the pure sizing decisions in
IndexScaler/MemoryManager/ShardManager, the AutoScaler coordinator, and the
generic ConnectionPool. Everything here runs offline: psutil is monkeypatched
at the function level (psutil.virtual_memory / psutil.cpu_percent) so no test
depends on the machine's real memory or CPU, and background-thread tests use
a tiny check_interval_seconds with a bounded wait, or drive _monitor_loop()
directly with a fake time.sleep that flips the running flag after one pass.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vectrixdb import ScalingStrategy, ScalingConfig, AutoScaler
from vectrixdb.core import scaling
from vectrixdb.core.scaling import (
    ConnectionPool,
    IndexScaler,
    MemoryManager,
    MetricsCollector,
    PerformanceMetrics,
    ResourceMonitor,
    ShardManager,
)


def _fake_vm(percent=50.0, used=8_000_000_000, available=8_000_000_000):
    return SimpleNamespace(percent=percent, used=used, available=available)


def _patch_psutil(
    monkeypatch, *, percent=50.0, used=8_000_000_000, available=8_000_000_000, cpu=1.0
):
    """Monkeypatch the two psutil functions scaling.py actually calls.

    Patches psutil.virtual_memory/cpu_percent themselves (the functions the
    module calls), not vectrixdb's reference to the psutil module, so this
    stays scoped to what scaling.py reads and is restored automatically by
    monkeypatch after the test.
    """
    monkeypatch.setattr(
        scaling.psutil, "virtual_memory", lambda: _fake_vm(percent, used, available)
    )
    monkeypatch.setattr(scaling.psutil, "cpu_percent", lambda interval=0.1: cpu)


class _FakeMonitor:
    """Stand-in for ResourceMonitor so AutoScaler tests never touch threads or psutil."""

    def __init__(self, metrics=None):
        self.started = False
        self.stopped = False
        self.callback = None
        self.queries = []
        self._metrics = metrics or PerformanceMetrics(timestamp=datetime.now(timezone.utc))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def on_metrics(self, callback):
        self.callback = callback

    def record_query(self, latency_ms):
        self.queries.append(latency_ms)

    def get_current_metrics(self):
        return self._metrics


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestScalingStrategy:
    """Test ScalingStrategy enum."""

    def test_scaling_strategies_exist(self):
        """Test scaling strategy enum values."""
        # Check strategies exist
        assert ScalingStrategy is not None
        assert hasattr(ScalingStrategy, "NONE")
        assert hasattr(ScalingStrategy, "BALANCED")


class TestScalingConfig:
    """Test ScalingConfig dataclass."""

    def test_config_creation(self):
        """Test scaling config creation."""
        config = ScalingConfig()
        assert config is not None

    def test_config_with_strategy(self):
        """Test config with strategy."""
        config = ScalingConfig(strategy=ScalingStrategy.NONE)
        assert config.strategy == ScalingStrategy.NONE


class TestAutoScaler:
    """Test AutoScaler class."""

    def test_auto_scaler_exists(self):
        """Test AutoScaler class exists."""
        assert AutoScaler is not None

    def test_auto_scaler_creation(self):
        """Test creating auto scaler."""
        config = ScalingConfig()
        scaler = AutoScaler(config)
        assert scaler is not None


class TestScalingIntegration:
    """Test scaling integration with VectrixDB."""

    def test_vectrixdb_with_scaling_config(self):
        """Test VectrixDB accepts scaling config."""
        from vectrixdb import VectrixDB

        scaling_config = ScalingConfig()
        db = VectrixDB(scaling_config=scaling_config)

        assert db is not None
        db.close()


# =============================================================================
# Extended coverage below: config.from_env, PerformanceMetrics, MetricsCollector,
# ResourceMonitor, IndexScaler, MemoryManager, ShardManager, AutoScaler decisions,
# ConnectionPool, and regression tests for real bugs found while writing these.
# =============================================================================


class TestScalingConfigFromEnv:
    def test_from_env_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("VECTRIX_SCALING_STRATEGY", raising=False)
        monkeypatch.delenv("VECTRIX_MEMORY_TARGET", raising=False)
        monkeypatch.delenv("VECTRIX_MAX_INDEX_SIZE", raising=False)

        config = ScalingConfig.from_env()

        assert config.strategy == ScalingStrategy.BALANCED
        assert config.memory_target_percent == 70.0
        assert config.max_index_capacity == 100_000_000

    def test_from_env_reads_overrides(self, monkeypatch):
        monkeypatch.setenv("VECTRIX_SCALING_STRATEGY", "aggressive")
        monkeypatch.setenv("VECTRIX_MEMORY_TARGET", "55.5")
        monkeypatch.setenv("VECTRIX_MAX_INDEX_SIZE", "12345")

        config = ScalingConfig.from_env()

        assert config.strategy == ScalingStrategy.AGGRESSIVE
        assert config.memory_target_percent == 55.5
        assert config.max_index_capacity == 12345

    def test_from_env_invalid_strategy_raises(self, monkeypatch):
        monkeypatch.setenv("VECTRIX_SCALING_STRATEGY", "not-a-strategy")

        with pytest.raises(ValueError):
            ScalingConfig.from_env()


class TestPerformanceMetrics:
    def test_avg_latency_with_queries(self):
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc), query_count=4, total_latency_ms=40.0
        )
        assert metrics.avg_latency_ms == 10.0

    def test_avg_latency_zero_queries_is_zero(self):
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc), query_count=0, total_latency_ms=0.0
        )
        assert metrics.avg_latency_ms == 0


class TestScalingMetricsCollector:
    def test_record_query_accumulates(self):
        collector = MetricsCollector()
        collector.record_query(10.0)
        collector.record_query(20.0)

        assert collector._query_count == 2
        assert collector._total_latency == 30.0

    def test_get_metrics_percentiles_hand_computed(self, monkeypatch):
        collector = MetricsCollector()
        for v in range(1, 11):  # sorted 1..10 ms
            collector.record_query(float(v))
        _patch_psutil(monkeypatch, percent=42.0, used=123, cpu=3.5)

        metrics = collector.get_metrics()

        n = 10
        latencies = list(range(1, 11))
        assert metrics.query_count == 10
        assert metrics.total_latency_ms == pytest.approx(55.0)
        assert metrics.p50_latency_ms == latencies[int(n * 0.5)]
        assert metrics.p95_latency_ms == latencies[int(n * 0.95)]
        assert metrics.p99_latency_ms == latencies[int(n * 0.99)]
        assert metrics.memory_percent == 42.0
        assert metrics.memory_used_bytes == 123
        assert metrics.cpu_percent == 3.5

    def test_get_metrics_empty_window_returns_zeros(self, monkeypatch):
        collector = MetricsCollector()
        _patch_psutil(monkeypatch, percent=1.0)

        metrics = collector.get_metrics()

        assert metrics.query_count == 0
        assert metrics.p50_latency_ms == 0
        assert metrics.p95_latency_ms == 0
        assert metrics.p99_latency_ms == 0

    def test_reset_clears_latencies_and_counters(self, monkeypatch):
        collector = MetricsCollector()
        collector.record_query(5.0)

        collector.reset()
        _patch_psutil(monkeypatch, percent=1.0)
        metrics = collector.get_metrics()

        assert metrics.query_count == 0
        assert metrics.total_latency_ms == 0


class TestResourceMonitor:
    def test_get_current_stats_shape_and_values(self, monkeypatch):
        monitor = ResourceMonitor(ScalingConfig())
        monitor.record_query(9.0)
        _patch_psutil(monkeypatch, percent=20.0, used=1000, cpu=2.0)

        stats = monitor.get_current_stats()

        assert stats["query_count"] == 1
        assert stats["avg_latency_ms"] == pytest.approx(9.0)
        assert stats["memory_percent"] == 20.0
        assert stats["memory_used_bytes"] == 1000
        assert stats["cpu_percent"] == 2.0
        assert set(stats) == {
            "timestamp",
            "query_count",
            "avg_latency_ms",
            "p50_latency_ms",
            "p95_latency_ms",
            "p99_latency_ms",
            "memory_used_bytes",
            "memory_percent",
            "cpu_percent",
            "index_size",
            "cache_hit_rate",
        }

    def test_monitor_loop_notifies_callbacks_then_resets(self, monkeypatch):
        """Drive _monitor_loop() directly instead of a real background thread.

        A fake time.sleep flips _running to False so the while-loop body runs
        exactly once: deterministic, and no real sleeping or threading needed.
        """
        config = ScalingConfig(check_interval_seconds=0)
        monitor = ResourceMonitor(config)
        _patch_psutil(monkeypatch, percent=11.0, cpu=1.0)
        monitor.record_query(4.0)
        seen = []
        monitor.on_metrics(seen.append)
        monitor._running = True

        def fake_sleep(_seconds):
            monitor._running = False

        monkeypatch.setattr(scaling.time, "sleep", fake_sleep)
        monitor._monitor_loop()

        assert len(seen) == 1
        assert seen[0].query_count == 1
        assert monitor.get_current_metrics().query_count == 0  # reset after the pass

    def test_monitor_loop_isolates_callback_errors(self, monkeypatch, caplog):
        config = ScalingConfig(check_interval_seconds=0)
        monitor = ResourceMonitor(config)
        _patch_psutil(monkeypatch, percent=11.0, cpu=1.0)
        good_calls = []

        def bad_callback(_metrics):
            raise RuntimeError("boom")

        monitor.on_metrics(bad_callback)
        monitor.on_metrics(good_calls.append)
        monitor._running = True
        monkeypatch.setattr(scaling.time, "sleep", lambda _s: setattr(monitor, "_running", False))

        with caplog.at_level(logging.ERROR):
            monitor._monitor_loop()

        assert len(good_calls) == 1
        assert "Metrics callback error" in caplog.text

    def test_monitor_loop_logs_and_continues_when_get_metrics_raises(self, monkeypatch, caplog):
        config = ScalingConfig(check_interval_seconds=0)
        monitor = ResourceMonitor(config)

        def boom():
            raise RuntimeError("psutil exploded")

        monkeypatch.setattr(scaling.psutil, "virtual_memory", boom)
        monitor._running = True
        monkeypatch.setattr(scaling.time, "sleep", lambda _s: setattr(monitor, "_running", False))

        with caplog.at_level(logging.ERROR):
            monitor._monitor_loop()  # must not raise

        assert "Monitor error" in caplog.text

    def test_start_is_idempotent_when_already_running(self, monkeypatch):
        config = ScalingConfig(check_interval_seconds=0.01)
        monitor = ResourceMonitor(config)
        _patch_psutil(monkeypatch, percent=5.0, cpu=1.0)

        monitor.start()
        try:
            first_thread = monitor._thread
            monitor.start()  # _running is already True: must be a no-op
            assert monitor._thread is first_thread
        finally:
            monitor.stop()

    def test_start_stop_lifecycle_uses_background_thread(self, monkeypatch):
        config = ScalingConfig(check_interval_seconds=0.01)
        monitor = ResourceMonitor(config)
        _patch_psutil(monkeypatch, percent=5.0, cpu=1.0)
        fired = threading.Event()
        monitor.on_metrics(lambda _m: fired.set())

        monitor.start()
        try:
            assert monitor._running is True
            assert fired.wait(timeout=2.0) is True
        finally:
            monitor.stop()

        assert monitor._running is False


class TestIndexScaler:
    @staticmethod
    def _config(**overrides):
        params = {
            "min_index_capacity": 1000,
            "max_index_capacity": 5000,
            "index_growth_factor": 2.0,
        }
        params.update(overrides)
        return ScalingConfig(**params)

    def test_get_capacity_defaults_to_configured_minimum(self):
        scaler = IndexScaler(self._config())
        assert scaler.get_capacity("unknown-collection") == 1000

    def test_set_and_get_capacity_round_trip(self):
        scaler = IndexScaler(self._config())
        scaler.set_capacity("c", 4000)
        assert scaler.get_capacity("c") == 4000

    def test_should_grow_true_at_exact_80_percent_boundary(self):
        scaler = IndexScaler(self._config())
        assert scaler.should_grow("c", current_count=800, current_capacity=1000) is True

    def test_should_grow_false_just_below_80_percent(self):
        scaler = IndexScaler(self._config())
        assert scaler.should_grow("c", current_count=799, current_capacity=1000) is False

    def test_calculate_new_capacity_applies_growth_factor(self):
        scaler = IndexScaler(self._config())
        assert scaler.calculate_new_capacity(1000) == 2000

    def test_calculate_new_capacity_clamps_to_configured_max(self):
        scaler = IndexScaler(self._config())
        assert scaler.calculate_new_capacity(4000) == 5000  # 8000 clamped down to 5000

    def test_should_shrink_false_when_capacity_at_minimum(self):
        scaler = IndexScaler(self._config())
        assert scaler.should_shrink("c", current_count=0, current_capacity=1000) is False

    def test_should_shrink_false_exactly_at_25_percent_boundary(self):
        scaler = IndexScaler(self._config())
        assert scaler.should_shrink("c", current_count=500, current_capacity=2000) is False

    def test_should_shrink_true_just_below_25_percent(self):
        scaler = IndexScaler(self._config())
        assert scaler.should_shrink("c", current_count=499, current_capacity=2000) is True

    def test_dead_band_between_25_and_80_percent_triggers_nothing(self):
        scaler = IndexScaler(self._config())
        for count in (500, 1000, 1500, 1599):
            assert scaler.should_grow("c", count, 2000) is False
            assert scaler.should_shrink("c", count, 2000) is False

    def test_calculate_shrink_capacity_doubles_current_count(self):
        scaler = IndexScaler(self._config())
        assert scaler.calculate_shrink_capacity(600) == 1200

    def test_calculate_shrink_capacity_floored_at_configured_minimum(self):
        scaler = IndexScaler(self._config())
        assert scaler.calculate_shrink_capacity(10) == 1000


class TestMemoryManager:
    def test_check_memory_below_watermark_reports_no_pressure(self, monkeypatch):
        mgr = MemoryManager(ScalingConfig(memory_high_watermark=85.0))
        called = []
        mgr.on_memory_pressure(lambda: called.append(1))
        _patch_psutil(monkeypatch, percent=50.0)

        percent, pressure = mgr.check_memory()

        assert percent == 50.0
        assert pressure is False
        assert called == []

    def test_check_memory_at_watermark_boundary_triggers_pressure(self, monkeypatch):
        mgr = MemoryManager(ScalingConfig(memory_high_watermark=85.0))
        called = []
        mgr.on_memory_pressure(lambda: called.append(1))
        _patch_psutil(monkeypatch, percent=85.0)

        _, pressure = mgr.check_memory()

        assert pressure is True
        assert called == [1]

    def test_check_memory_just_below_watermark_does_not_trigger(self, monkeypatch):
        mgr = MemoryManager(ScalingConfig(memory_high_watermark=85.0))
        called = []
        mgr.on_memory_pressure(lambda: called.append(1))
        _patch_psutil(monkeypatch, percent=84.999)

        _, pressure = mgr.check_memory()

        assert pressure is False
        assert called == []

    def test_pressure_callback_error_is_isolated_from_other_callbacks(self, monkeypatch, caplog):
        mgr = MemoryManager(ScalingConfig(memory_high_watermark=50.0))
        calls = []

        def bad():
            raise RuntimeError("boom")

        mgr.on_memory_pressure(bad)
        mgr.on_memory_pressure(lambda: calls.append(1))
        _patch_psutil(monkeypatch, percent=99.0)

        with caplog.at_level(logging.ERROR):
            _, pressure = mgr.check_memory()

        assert pressure is True
        assert calls == [1]
        assert "Memory pressure callback error" in caplog.text

    def test_get_available_memory_mb_converts_bytes(self, monkeypatch):
        mgr = MemoryManager(ScalingConfig())
        _patch_psutil(monkeypatch, available=2 * 1024 * 1024)

        assert mgr.get_available_memory_mb() == pytest.approx(2.0)

    def test_estimate_vector_memory_hand_computed(self):
        mgr = MemoryManager(ScalingConfig())

        # 1000 vectors x 100 dims x 4 bytes = 400_000 bytes of raw vector data,
        # plus the fixed 20% index overhead the function adds on top.
        vector_bytes = 1000 * 100 * 4
        expected_mb = (vector_bytes + vector_bytes * 0.2) / (1024 * 1024)

        assert mgr.estimate_vector_memory(dimension=100, count=1000) == pytest.approx(expected_mb)

    def test_estimate_vector_memory_scales_linearly_with_dtype_bytes(self):
        mgr = MemoryManager(ScalingConfig())
        base = mgr.estimate_vector_memory(dimension=10, count=10, dtype_bytes=4)
        doubled = mgr.estimate_vector_memory(dimension=10, count=10, dtype_bytes=8)

        assert doubled == pytest.approx(base * 2)


class TestShardManager:
    def test_get_shard_count_defaults_to_one_unsharded_collection(self):
        mgr = ShardManager(ScalingConfig())
        assert mgr.get_shard_count("c") == 1

    def test_get_shard_for_id_with_single_shard_returns_the_collection_name(self):
        mgr = ShardManager(ScalingConfig())
        assert mgr.get_shard_for_id("c", "any-id") == "c"

    def test_add_shard_names_sequentially_and_grows_the_count(self):
        mgr = ShardManager(ScalingConfig(max_shards=16))

        first = mgr.add_shard("c")
        assert first == "c_shard_1"
        assert mgr.get_shard_count("c") == 2

        second = mgr.add_shard("c")
        assert second == "c_shard_2"
        assert mgr.get_shard_count("c") == 3

    def test_add_shard_returns_none_once_max_shards_reached(self):
        mgr = ShardManager(ScalingConfig(max_shards=2))

        first = mgr.add_shard("c")
        assert first == "c_shard_1"
        assert mgr.get_shard_count("c") == 2

        assert mgr.add_shard("c") is None
        assert mgr.get_shard_count("c") == 2

    def test_get_shard_for_id_multi_shard_matches_the_hash_formula(self):
        mgr = ShardManager(ScalingConfig(max_shards=16))
        mgr.add_shard("c")
        mgr.add_shard("c")
        shards = mgr.get_all_shards("c")
        assert len(shards) == 3

        expected = shards[hash("point-1") % len(shards)]
        assert mgr.get_shard_for_id("c", "point-1") == expected

    def test_get_all_shards_returns_an_independent_copy(self):
        mgr = ShardManager(ScalingConfig())
        shards = mgr.get_all_shards("c")
        shards.append("intruder")

        assert mgr.get_all_shards("c") == ["c"]

    def test_should_add_shard_false_when_sharding_disabled(self):
        mgr = ShardManager(ScalingConfig(enable_sharding=False))
        assert mgr.should_add_shard("c", current_count=10_000_000) is False

    def test_should_add_shard_true_above_threshold(self):
        mgr = ShardManager(ScalingConfig(enable_sharding=True, max_shards=16))
        assert (
            mgr.should_add_shard("c", current_count=1_000_001, vectors_per_shard=1_000_000) is True
        )

    def test_should_add_shard_false_at_exact_threshold(self):
        mgr = ShardManager(ScalingConfig(enable_sharding=True, max_shards=16))
        assert (
            mgr.should_add_shard("c", current_count=1_000_000, vectors_per_shard=1_000_000) is False
        )

    def test_should_add_shard_false_when_already_at_max_shards(self):
        mgr = ShardManager(ScalingConfig(enable_sharding=True, max_shards=1))
        assert mgr.should_add_shard("c", current_count=10_000_000, vectors_per_shard=1) is False


class TestAutoScalerDecisions:
    @staticmethod
    def _scaler(**config_overrides):
        params = {
            "min_index_capacity": 1000,
            "max_index_capacity": 5000,
            "index_growth_factor": 2.0,
        }
        params.update(config_overrides)
        config = ScalingConfig(**params)
        return AutoScaler(config, resource_monitor=_FakeMonitor()), config

    def test_should_scale_index_grows_and_records_a_decision(self):
        scaler, _ = self._scaler()
        scaler.index_scaler.set_capacity("c", 1000)

        should, new_capacity = scaler.should_scale_index("c", 800)

        assert should is True
        assert new_capacity == 2000
        decision = scaler._decisions[-1]
        assert decision["action"] == "grow_index"
        assert decision["collection"] == "c"
        assert decision["old_value"] == 1000
        assert decision["new_value"] == 2000
        assert "timestamp" in decision

    def test_should_scale_index_shrinks_and_records_a_decision(self):
        scaler, _ = self._scaler()
        scaler.index_scaler.set_capacity("c", 2000)

        should, new_capacity = scaler.should_scale_index("c", 100)

        assert should is True
        assert new_capacity == 1000  # max(100 * 2, min_index_capacity=1000)
        decision = scaler._decisions[-1]
        assert decision["action"] == "shrink_index"

    def test_should_scale_index_is_a_noop_in_the_dead_band(self):
        scaler, _ = self._scaler()
        scaler.index_scaler.set_capacity("c", 2000)

        should, capacity = scaler.should_scale_index("c", 1000)

        assert should is False
        assert capacity == 2000
        assert len(scaler._decisions) == 0

    def test_register_index_seeds_the_minimum_capacity(self):
        scaler, config = self._scaler()
        scaler.register_index("c", index=object())
        assert scaler.index_scaler.get_capacity("c") == config.min_index_capacity

    def test_unregister_index_drops_tracking(self):
        scaler, config = self._scaler()
        scaler.register_index("c", index=object())

        scaler.unregister_index("c")

        assert "c" not in scaler.index_scaler._current_capacity
        assert scaler.index_scaler.get_capacity("c") == config.min_index_capacity

    def test_record_query_forwards_to_the_monitor(self):
        monitor = _FakeMonitor()
        scaler = AutoScaler(ScalingConfig(), resource_monitor=monitor)

        scaler.record_query(12.5)

        assert monitor.queries == [12.5]

    def test_start_is_a_noop_when_strategy_is_none(self):
        monitor = _FakeMonitor()
        scaler = AutoScaler(ScalingConfig(strategy=ScalingStrategy.NONE), resource_monitor=monitor)

        scaler.start()

        assert monitor.started is False

    def test_start_delegates_to_the_monitor_when_scaling_is_enabled(self):
        monitor = _FakeMonitor()
        scaler = AutoScaler(
            ScalingConfig(strategy=ScalingStrategy.BALANCED), resource_monitor=monitor
        )

        scaler.start()

        assert monitor.started is True

    def test_stop_always_delegates_to_the_monitor(self):
        monitor = _FakeMonitor()
        scaler = AutoScaler(ScalingConfig(strategy=ScalingStrategy.NONE), resource_monitor=monitor)

        scaler.stop()

        assert monitor.stopped is True

    def test_check_memory_pressure_reflects_the_memory_manager(self, monkeypatch):
        scaler, _ = self._scaler(memory_high_watermark=80.0)

        _patch_psutil(monkeypatch, percent=95.0)
        assert scaler.check_memory_pressure() is True

        _patch_psutil(monkeypatch, percent=10.0)
        assert scaler.check_memory_pressure() is False

    def test_get_status_reports_strategy_and_metrics(self):
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc),
            query_count=4,
            total_latency_ms=40.0,
            p95_latency_ms=12.5,
            memory_percent=33.0,
            cpu_percent=8.0,
        )
        scaler = AutoScaler(
            ScalingConfig(strategy=ScalingStrategy.AGGRESSIVE),
            resource_monitor=_FakeMonitor(metrics=metrics),
        )

        status = scaler.get_status()

        assert status["strategy"] == "aggressive"
        assert status["metrics"]["query_count"] == 4
        assert status["metrics"]["avg_latency_ms"] == pytest.approx(10.0)
        assert status["metrics"]["p95_latency_ms"] == 12.5
        assert status["recent_decisions"] == []

    def test_get_status_recent_decisions_keeps_only_the_last_ten(self):
        scaler, _ = self._scaler()
        for i in range(15):
            scaler._record_decision(f"c{i}", "grow_index", 1, 2)

        recent = scaler.get_status()["recent_decisions"]

        assert len(recent) == 10
        assert recent[0]["collection"] == "c5"
        assert recent[-1]["collection"] == "c14"

    def test_on_metrics_logs_warnings_above_both_thresholds(self, caplog):
        scaler, _ = self._scaler(latency_target_p95=50.0, memory_high_watermark=85.0)
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc), p95_latency_ms=60.0, memory_percent=90.0
        )

        with caplog.at_level(logging.WARNING, logger="vectrixdb.core.scaling"):
            scaler._on_metrics(metrics)

        assert "P95 latency" in caplog.text
        assert "Memory usage high" in caplog.text

    def test_on_metrics_is_silent_below_both_thresholds(self, caplog):
        scaler, _ = self._scaler(latency_target_p95=50.0, memory_high_watermark=85.0)
        metrics = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc), p95_latency_ms=5.0, memory_percent=5.0
        )
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="vectrixdb.core.scaling"):
            scaler._on_metrics(metrics)

        assert caplog.text == ""


class TestConnectionPool:
    def test_pre_creates_the_minimum_connections(self):
        created = []

        def factory():
            conn = _FakeConn()
            created.append(conn)
            return conn

        pool = ConnectionPool(ScalingConfig(min_connections=3, max_connections=10), factory)

        assert len(created) == 3
        assert pool.stats == {"available": 3, "in_use": 0, "total": 3}

    def test_acquire_reuses_a_pooled_connection_before_calling_the_factory(self):
        created = []

        def factory():
            conn = _FakeConn()
            created.append(conn)
            return conn

        pool = ConnectionPool(ScalingConfig(min_connections=1, max_connections=10), factory)

        conn = pool.acquire()

        assert conn is created[0]
        assert len(created) == 1
        assert pool.stats == {"available": 0, "in_use": 1, "total": 1}

    def test_acquire_beyond_the_pool_calls_the_factory_and_release_recycles_it(self):
        created = []

        def factory():
            conn = _FakeConn()
            created.append(conn)
            return conn

        pool = ConnectionPool(ScalingConfig(min_connections=0, max_connections=10), factory)

        conn = pool.acquire()
        assert len(created) == 1

        pool.release(conn)
        assert pool.stats == {"available": 1, "in_use": 0, "total": 1}

        conn2 = pool.acquire()
        assert conn2 is conn
        assert len(created) == 1

    def test_acquire_raises_timeout_error_once_the_pool_is_exhausted(self):
        pool = ConnectionPool(ScalingConfig(min_connections=1, max_connections=1), _FakeConn)
        pool.acquire()

        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0.05)

    def test_close_all_closes_every_connection_even_if_one_close_raises(self, caplog):
        class _BadConn:
            def close(self):
                raise RuntimeError("boom")

        supply = iter([_BadConn(), _FakeConn()])
        pool = ConnectionPool(
            ScalingConfig(min_connections=0, max_connections=5), lambda: next(supply)
        )
        first = pool.acquire()  # the bad connection
        second = pool.acquire()  # the good connection
        pool.release(first)
        pool.release(second)

        with caplog.at_level(logging.DEBUG):
            pool.close_all()  # must not raise, and must still close `second`

        assert second.closed is True
        assert pool.stats == {"available": 0, "in_use": 0, "total": 0}

    def test_stats_reports_available_in_use_and_total(self):
        pool = ConnectionPool(ScalingConfig(min_connections=2, max_connections=5), _FakeConn)
        pool.acquire()

        assert pool.stats == {"available": 1, "in_use": 1, "total": 2}


class TestScalingKnownBugs:
    """Regression tests documenting real bugs found while writing this suite.

    Each is marked xfail(strict=True) with a one-line reason. If a fix lands,
    the assertion starts passing, strict mode turns that into a failure, and
    that failure is the signal to delete the xfail marker.
    """

    def test_acquire_timeout_zero_should_fail_fast_instead_of_using_the_default(self):
        pool = ConnectionPool(
            ScalingConfig(min_connections=0, max_connections=1, connection_timeout=0.2),
            _FakeConn,
        )
        pool.acquire()  # take the only permit so the next acquire must wait

        start = time.perf_counter()
        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.05, f"acquire(timeout=0) took {elapsed:.3f}s; it should not block at all"
