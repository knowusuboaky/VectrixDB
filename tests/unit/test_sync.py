"""
Tests for VectrixSync functionality.
"""

import pytest
import time

from vectrixdb import VectrixDB, VectrixSync, SyncResult, SyncStatus


class TestVectrixSync:
    """Test VectrixSync operations."""

    def test_sync_init(self):
        """Test creating a sync instance."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)

        assert sync.source is source
        assert sync.target is target
        assert sync.sync_collections is True
        assert sync.sync_documents is True

        source.close()
        target.close()

    def test_sync_init_options(self):
        """Test sync with custom options."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(
            source=source,
            target=target,
            sync_collections=True,
            sync_documents=False,
            batch_size=500
        )

        assert sync.sync_collections is True
        assert sync.sync_documents is False
        assert sync.batch_size == 500

        source.close()
        target.close()

    def test_full_sync_empty(self):
        """Test full sync with empty database."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)
        result = sync.full()

        assert isinstance(result, SyncResult)
        assert result.success is True
        assert result.rows_synced == 0
        assert len(result.errors) == 0

        source.close()
        target.close()

    def test_sync_status_initial(self):
        """Test getting sync status initially."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)

        # Check status before sync
        status = sync.status()
        assert isinstance(status, SyncStatus)
        assert status.last_sync is None
        assert status.is_running is False

        source.close()
        target.close()

    def test_sync_status_after_sync(self):
        """Test getting sync status after sync."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)
        sync.full()

        # Check status after sync
        status = sync.status()
        assert status.last_sync is not None

        source.close()
        target.close()

    def test_incremental_sync(self):
        """Test incremental sync."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)
        sync.full()

        # Incremental sync
        result = sync.incremental()
        assert result.success is True

        source.close()
        target.close()

    def test_auto_sync(self):
        """Test auto sync method."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)

        # Auto sync should do full sync first time
        result = sync.auto(interval_minutes=1)

        assert result.success is True
        assert sync._scheduler_thread is not None
        assert sync._scheduler_thread.is_alive()

        # Stop scheduler
        sync.stop_scheduler()

        source.close()
        target.close()

    def test_scheduler_start_stop(self):
        """Test starting and stopping scheduler."""
        source = VectrixDB()
        target = VectrixDB()

        sync = VectrixSync(source=source, target=target)

        # Start scheduler
        sync.start_scheduler(interval_minutes=1)
        assert sync._scheduler_thread is not None
        assert sync._scheduler_thread.is_alive()

        # Stop scheduler
        sync.stop_scheduler()
        time.sleep(0.2)  # Give thread time to stop

        source.close()
        target.close()


class TestSyncResult:
    """Test SyncResult dataclass."""

    def test_sync_result_creation(self):
        """Test creating a SyncResult."""
        result = SyncResult(
            success=True,
            rows_synced=100,
            collections_synced=["coll1", "coll2"],
            documents_synced=5,
            nodes_synced=25,
            duration_seconds=1.5
        )

        assert result.success is True
        assert result.rows_synced == 100
        assert len(result.collections_synced) == 2
        assert result.documents_synced == 5
        assert result.nodes_synced == 25
        assert result.duration_seconds == 1.5
        assert result.timestamp is not None

    def test_sync_result_with_errors(self):
        """Test SyncResult with errors."""
        result = SyncResult(
            success=False,
            rows_synced=50,
            collections_synced=["coll1"],
            documents_synced=0,
            nodes_synced=0,
            duration_seconds=0.5,
            errors=["Error 1", "Error 2"]
        )

        assert result.success is False
        assert len(result.errors) == 2


class TestSyncStatus:
    """Test SyncStatus dataclass."""

    def test_sync_status_creation(self):
        """Test creating a SyncStatus."""
        status = SyncStatus(
            last_sync="2024-01-01T00:00:00",
            rows_synced=100,
            lag_seconds=30.0,
            is_running=False,
            collections={"coll1": {"source": 100, "target": 100, "pending": 0}}
        )

        assert status.last_sync == "2024-01-01T00:00:00"
        assert status.rows_synced == 100
        assert status.lag_seconds == 30.0
        assert status.is_running is False
        assert "coll1" in status.collections
