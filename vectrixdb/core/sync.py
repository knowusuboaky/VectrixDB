"""
VectrixSync - Sync data between storage backends.

Primary use case: Delta Lake (governed source) → Lakebase (fast search target)

Author: VectrixDB Team
"""

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import VectrixDB


@dataclass
class SyncResult:
    """Result of a sync operation."""
    success: bool
    rows_synced: int
    collections_synced: List[str]
    documents_synced: int
    nodes_synced: int
    duration_seconds: float
    errors: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class SyncStatus:
    """Current sync status."""
    last_sync: Optional[str]
    rows_synced: int
    lag_seconds: float
    is_running: bool
    collections: Dict[str, Dict[str, int]]


class VectrixSync:
    """
    Sync data between VectrixDB instances (typically Delta Lake → Lakebase).

    Usage:
        from vectrixdb import VectrixDB, VectrixSync

        # Source: Delta Lake (governed, slow search)
        delta = VectrixDB.with_delta_lake(
            workspace_url="https://adb-123.azuredatabricks.net",
            token="dapi...",
            catalog="main",
            schema="vectrixdb"
        )

        # Target: Lakebase (fast search)
        lakebase = VectrixDB.with_lakebase(
            host="abc.lakebase.databricks.com",
            token="dapi...",
            database="vectrixdb"
        )

        # Sync
        sync = VectrixSync(source=delta, target=lakebase)
        sync.full()  # First time: full sync
        sync.incremental()  # Subsequent: only changes
    """

    def __init__(
        self,
        source: "VectrixDB",
        target: "VectrixDB",
        sync_collections: bool = True,
        sync_documents: bool = True,
        batch_size: int = 1000,
    ):
        """
        Initialize sync between two VectrixDB instances.

        Args:
            source: Source database (typically Delta Lake)
            target: Target database (typically Lakebase)
            sync_collections: Whether to sync vector collections
            sync_documents: Whether to sync document index
            batch_size: Batch size for sync operations
        """
        self.source = source
        self.target = target
        self.sync_collections = sync_collections
        self.sync_documents = sync_documents
        self.batch_size = batch_size

        self._last_sync: Optional[str] = None
        self._is_running = False
        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop_scheduler = threading.Event()
        self._lock = threading.RLock()

    def full(self, collections: Optional[List[str]] = None) -> SyncResult:
        """
        Perform full sync from source to target.

        Copies ALL data. Use for initial sync or disaster recovery.

        Args:
            collections: Optional list of collection names to sync.
                        If None, syncs all collections.

        Returns:
            SyncResult with sync statistics
        """
        start_time = time.time()
        errors = []
        rows_synced = 0
        collections_synced = []
        documents_synced = 0
        nodes_synced = 0

        with self._lock:
            self._is_running = True

        try:
            # Sync collections
            if self.sync_collections:
                source_collections = self.source.list_collections()
                for coll_info in source_collections:
                    coll_name = coll_info.name
                    if collections and coll_name not in collections:
                        continue

                    try:
                        # Create collection in target if not exists
                        target_collections = [c.name for c in self.target.list_collections()]
                        if coll_name not in target_collections:
                            self.target.create_collection(
                                name=coll_name,
                                dimension=coll_info.dimension,
                                metric=coll_info.metric,
                            )

                        # Get source collection
                        source_coll = self.source.get_collection(coll_name)
                        target_coll = self.target.get_collection(coll_name)

                        if source_coll and target_coll:
                            # Iterate through all points and sync
                            batch = []
                            for point_id, point_data in source_coll._storage.iterate(coll_name, self.batch_size):
                                batch.append((point_id, point_data))
                                if len(batch) >= self.batch_size:
                                    target_coll._storage.insert_batch(coll_name, batch)
                                    rows_synced += len(batch)
                                    batch = []

                            # Insert remaining
                            if batch:
                                target_coll._storage.insert_batch(coll_name, batch)
                                rows_synced += len(batch)

                            collections_synced.append(coll_name)

                    except Exception as e:
                        errors.append(f"Collection {coll_name}: {str(e)}")

            # Sync documents
            if self.sync_documents:
                try:
                    source_docs = self.source.documents.list_documents()
                    for doc in source_docs:
                        # Sync document info
                        doc_data = {
                            "doc_id": doc.doc_id,
                            "title": doc.title,
                            "doc_type": doc.doc_type.value if hasattr(doc.doc_type, 'value') else str(doc.doc_type),
                            "page_count": doc.page_count,
                            "section_count": doc.section_count,
                            "node_count": doc.node_count,
                            "metadata": doc.metadata,
                        }
                        self.target._storage.save_document(doc_data)
                        documents_synced += 1

                        # Sync nodes
                        source_nodes = self.source.documents.get_document_nodes(doc.doc_id)
                        for node in source_nodes:
                            node_data = {
                                "node_id": node.node_id,
                                "doc_id": node.doc_id,
                                "parent_id": node.parent_id,
                                "level": node.level,
                                "title": node.title,
                                "text": node.text,
                                "summary": node.summary,
                                "page_num": node.page_num,
                                "position": node.position,
                                "metadata": node.metadata,
                            }
                            self.target._storage.save_node(node_data)
                            nodes_synced += 1

                except Exception as e:
                    errors.append(f"Documents: {str(e)}")

            self._last_sync = datetime.now().isoformat()

        finally:
            with self._lock:
                self._is_running = False

        duration = time.time() - start_time

        return SyncResult(
            success=len(errors) == 0,
            rows_synced=rows_synced,
            collections_synced=collections_synced,
            documents_synced=documents_synced,
            nodes_synced=nodes_synced,
            duration_seconds=duration,
            errors=errors,
        )

    def incremental(self, since: Optional[str] = None) -> SyncResult:
        """
        Perform incremental sync - only sync changes since last sync.

        Note: This requires source to track updated_at timestamps.
        For Delta Lake, this uses the _sync_watermark.

        Args:
            since: ISO timestamp to sync from. If None, uses last sync time.

        Returns:
            SyncResult with sync statistics
        """
        # For now, incremental is same as full
        # TODO: Implement proper incremental sync using Delta Lake CDF
        # or updated_at filtering
        return self.full()

    def collection(self, name: str) -> SyncResult:
        """
        Sync a specific collection.

        Args:
            name: Collection name to sync

        Returns:
            SyncResult with sync statistics
        """
        return self.full(collections=[name])

    def status(self) -> SyncStatus:
        """
        Get current sync status.

        Returns:
            SyncStatus with current state
        """
        lag_seconds = 0.0
        if self._last_sync:
            last_sync_time = datetime.fromisoformat(self._last_sync)
            lag_seconds = (datetime.now() - last_sync_time).total_seconds()

        # Get collection sync status
        collections = {}
        try:
            source_collections = self.source.list_collections()
            target_collections = {c.name: c for c in self.target.list_collections()}

            for coll in source_collections:
                source_count = coll.count
                target_count = target_collections.get(coll.name, None)
                target_count = target_count.count if target_count else 0
                collections[coll.name] = {
                    "source": source_count,
                    "target": target_count,
                    "pending": max(0, source_count - target_count),
                }
        except:
            pass

        return SyncStatus(
            last_sync=self._last_sync,
            rows_synced=sum(c.get("target", 0) for c in collections.values()),
            lag_seconds=lag_seconds,
            is_running=self._is_running,
            collections=collections,
        )

    def auto(self, interval_minutes: int = 5) -> SyncResult:
        """
        One-liner sync setup: full sync if needed, then start scheduler.

        Recommended for production use. Call once at app startup.

        Args:
            interval_minutes: Minutes between sync runs (default: 5)

        Returns:
            SyncResult from initial sync (if performed)

        Usage:
            sync = VectrixSync(delta, lakebase)
            sync.auto()  # That's it! Syncs now and every 5 minutes

            # Just write to Delta Lake
            delta.get_collection("docs").add(...)  # Auto-synced!
        """
        result = None

        # Check if we need initial full sync
        status = self.status()
        needs_full_sync = (
            status.last_sync is None or  # Never synced
            any(c.get("pending", 0) > 0 for c in status.collections.values())  # Has pending data
        )

        if needs_full_sync:
            result = self.full()
        else:
            # Create empty result for "already synced" case
            result = SyncResult(
                success=True,
                rows_synced=0,
                collections_synced=[],
                documents_synced=0,
                nodes_synced=0,
                duration_seconds=0.0,
            )

        # Start scheduler for ongoing sync
        self.start_scheduler(interval_minutes)

        return result

    def start_scheduler(self, interval_minutes: int = 5) -> None:
        """
        Start background scheduler for automatic incremental syncs.

        Args:
            interval_minutes: Minutes between sync runs
        """
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return  # Already running

        self._stop_scheduler.clear()

        def scheduler_loop():
            while not self._stop_scheduler.is_set():
                try:
                    self.incremental()
                except Exception as e:
                    print(f"Sync error: {e}")

                # Wait for interval or stop signal
                self._stop_scheduler.wait(timeout=interval_minutes * 60)

        self._scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        """Stop the background scheduler."""
        self._stop_scheduler.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
            self._scheduler_thread = None

    def start_cdc(self) -> None:
        """
        Start Change Data Capture for near real-time sync.

        Uses Delta Lake Change Data Feed to capture INSERT, UPDATE, DELETE
        and replicate to target.

        Note: Requires Delta Lake CDF to be enabled on source tables.
        """
        # TODO: Implement CDC using Delta Lake Change Data Feed
        # This would require:
        # 1. Enable CDF on Delta tables: TBLPROPERTIES (delta.enableChangeDataFeed = true)
        # 2. Query table_changes() function
        # 3. Apply changes to target
        raise NotImplementedError(
            "CDC sync not yet implemented. Use incremental() or start_scheduler() instead."
        )


# Convenience function
def create_sync(
    source: "VectrixDB",
    target: "VectrixDB",
    **kwargs
) -> VectrixSync:
    """
    Create a sync instance between two VectrixDB databases.

    Args:
        source: Source database (typically Delta Lake)
        target: Target database (typically Lakebase)
        **kwargs: Additional arguments for VectrixSync

    Returns:
        VectrixSync instance
    """
    return VectrixSync(source=source, target=target, **kwargs)
