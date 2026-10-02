"""
VectrixSync - Sync data between storage backends.

Primary use case: Delta Lake (governed source) → Lakebase (fast search target)

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
import logging

from .._time import ensure_aware, parse_iso, utcnow, utcnow_iso
from typing import Any, Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING


__all__ = [
    "SyncResult",
    "SyncStatus",
    "VectrixSync",
    "create_sync",
]


# ============================================================================
# SETTINGS: the logger
# ============================================================================
#
# One logger for the sync's lines.

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .database import VectrixDB


# ============================================================================
# THE RESULT, THE STATUS, AND THE SYNC
# ============================================================================
#
# INPUT   a source and a target database
# OUTPUT  what a sync did; where it stands; data synced from one to the other,
#         typically Delta Lake to Lakebase; a sync made between two databases
#
# The governed source stays the source; the fast target is rebuilt from it.


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
    timestamp: str = field(default_factory=utcnow_iso)
    #: False when a cutoff was asked for but no row carried a timestamp to
    #: compare against, so everything was copied. True for a full sync, which
    #: never intended to filter, and for an incremental one that could.
    filtered: bool = True


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

    Scope: this moves rows between storage backends. It is for databases whose
    backend is the store and whose search runs there, which is what the pair
    above is. Against a local collection the rows land in the backend but the
    collection's own index and count are not rebuilt, so a local target still
    reads empty. Use vectrixdb.snapshot.export() and import_snapshot() to move
    a local collection.
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
        # The watermark incremental() compares rows against. None until a run
        # succeeds, so a fresh instance copies everything once.
        self._last_sync_at: Optional[datetime] = None
        self._is_running = False
        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop_scheduler = threading.Event()
        self._lock = threading.RLock()

    @staticmethod
    def _changed_since(point_data: Dict[str, Any], since: Optional[datetime]) -> bool:
        """Whether a row has changed since a cutoff.

        A row with no usable timestamp is always copied: the safe answer when
        the source cannot say is to move it, not to skip it.
        """
        if since is None:
            return True
        stamp = point_data.get("updated_at") or point_data.get("created_at")
        if stamp is None:
            return True
        if isinstance(stamp, str):
            try:
                stamp = parse_iso(stamp)
            except (TypeError, ValueError):
                return True
        if not isinstance(stamp, datetime):
            return True
        return ensure_aware(stamp) > since

    def _read_points(self, backend: Any, collection: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Every point in a collection, whatever the backend offers.

        ``iterate`` streams and only Delta Lake, OpenSearch and Aurora have
        it. ``scan`` is on BaseStorage, so every backend answers it, and it
        pages. Preferring iterate keeps the streaming backends streaming.
        """
        streamer = getattr(backend, "iterate", None)
        if callable(streamer):
            yield from streamer(collection, self.batch_size)
            return

        offset = 0
        while True:
            page = list(backend.scan(collection, self.batch_size, offset))
            if not page:
                return
            yield from page
            if len(page) < self.batch_size:
                return
            offset += len(page)

    def full(
        self,
        collections: Optional[List[str]] = None,
        since: Optional[datetime] = None,
    ) -> SyncResult:
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
        rows_with_a_timestamp = 0
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
                    # ``is not None``, not truthiness: full(collections=[])
                    # asks for nothing to be synced, and reading the empty
                    # list as "no selection given" copied every collection.
                    if collections is not None and coll_name not in collections:
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

                        # ``is not None``, not truthiness: a Collection is
                        # sized, and the target was just created, so an empty
                        # one is falsy and the sync used to copy nothing and
                        # report success.
                        if source_coll is not None and target_coll is not None:
                            batch = []
                            for point_id, point_data in self._read_points(
                                source_coll._storage_backend, coll_name
                            ):
                                if point_data.get("updated_at") or point_data.get("created_at"):
                                    rows_with_a_timestamp += 1
                                elif since is not None:
                                    # Nothing to compare against, so copy it.
                                    pass
                                if not self._changed_since(point_data, since):
                                    continue
                                batch.append((point_id, point_data))
                                if len(batch) >= self.batch_size:
                                    target_coll._storage_backend.insert_batch(coll_name, batch)
                                    rows_synced += len(batch)
                                    batch = []

                            # Insert remaining
                            if batch:
                                target_coll._storage_backend.insert_batch(coll_name, batch)
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
                            "doc_type": doc.doc_type.value
                            if hasattr(doc.doc_type, "value")
                            else str(doc.doc_type),
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

            self._last_sync = utcnow_iso()

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
            filtered=since is None or rows_with_a_timestamp > 0,
        )

    def incremental(self, since: Optional[str] = None) -> SyncResult:
        """
        Copy only the rows that have changed since a cutoff.

        The cutoff is ``since`` when given, otherwise the time of the last
        successful run on this instance; the first run on a fresh instance has
        no watermark and copies everything. Rows are compared on their
        ``updated_at``, falling back to ``created_at``. A row carrying neither
        is always copied, because the safe answer when the source cannot say
        is to move it.

        This used to return ``full()`` and ignore ``since`` entirely, so a
        scheduled sync re-copied the whole dataset on every tick.

        Args:
            since: ISO timestamp to sync from. If None, uses the last run.

        Returns:
            SyncResult with sync statistics
        """
        cutoff: Optional[datetime]
        if since is not None:
            parsed = parse_iso(since)
            if parsed is None:
                raise ValueError(f"since is not an ISO timestamp: {since!r}")
            cutoff = ensure_aware(parsed)
        else:
            cutoff = self._last_sync_at

        started = utcnow()
        result = self.full(since=cutoff)
        if result.success:
            # Only move the watermark on a clean run, or a failure would be
            # skipped over on the next pass.
            self._last_sync_at = started
        return result

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
            last_sync_time = parse_iso(self._last_sync)
            if last_sync_time is not None:
                lag_seconds = (utcnow() - last_sync_time).total_seconds()

        # Get collection sync status
        collections: Dict[str, Dict[str, int]] = {}
        try:
            source_collections = self.source.list_collections()
            target_collections = {c.name: c for c in self.target.list_collections()}

            for coll in source_collections:
                source_count = coll.count
                target_info = target_collections.get(coll.name, None)
                target_count = target_info.count if target_info else 0
                collections[coll.name] = {
                    "source": source_count,
                    "target": target_count,
                    "pending": max(0, source_count - target_count),
                }
        except Exception as exc:
            # Per-collection counts are advisory; the status object is still
            # meaningful without them. Record why rather than discarding it.
            logger.warning("could not compute per-collection sync status: %s", exc)

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
            status.last_sync is None  # Never synced
            or any(c.get("pending", 0) > 0 for c in status.collections.values())  # Has pending data
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
                    logger.error("sync failed: %s", e, exc_info=True)

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
def create_sync(source: "VectrixDB", target: "VectrixDB", **kwargs) -> VectrixSync:
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
