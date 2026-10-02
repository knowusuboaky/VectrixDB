"""VectrixSync.cdc() carries deletes to the target.

full() and incremental() only ever copied, so a row deleted or revoked in the
governed source stayed searchable in the target for good. cdc() reads Delta
Lake's change data feed where there is one and compares ids where there is
not, and either way a row the source no longer has leaves the target.

The end to end tests run the real DeltaLakeStorage SQL against
fake_databricks, which records a change feed the way Delta does; the rest use
small fakes, because what is under test there is the sync loop.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest

from vectrixdb.core.storage import InMemoryStorage, StorageBackend, StorageConfig
from vectrixdb.core.sync import VectrixSync

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Backend:
    """A backend with scan() only and no change feed, like SQLite or Cosmos."""

    def __init__(self, rows: Optional[Dict[str, Dict[str, Any]]] = None):
        self.rows: Dict[str, Dict[str, Any]] = dict(rows or {})

    def scan(self, collection, limit=100, offset=0, filter_func=None):
        yield from list(self.rows.items())[offset : offset + limit]

    def insert_batch(self, collection, items):
        for point_id, data in items:
            self.rows[point_id] = dict(data)
        return len(items)

    def delete_batch(self, collection, ids):
        return sum(self.rows.pop(i, None) is not None for i in ids)


class _FeedBackend(_Backend):
    """A backend with a change feed: a version and a list of changes."""

    def __init__(self, rows=None):
        super().__init__(rows)
        self.version = 0
        self.log: List[Tuple[int, str, str, Optional[Dict[str, Any]]]] = []
        self.feed_broken = False
        self.asked: List[Tuple[int, int]] = []

    def put(self, point_id, data):
        self.version += 1
        self.rows[point_id] = data
        self.log.append((self.version, "upsert", point_id, data))

    def remove(self, point_id):
        self.version += 1
        del self.rows[point_id]
        self.log.append((self.version, "delete", point_id, None))

    def current_version(self, collection):
        return self.version

    def changes(self, collection, start, end):
        self.asked.append((start, end))
        if self.feed_broken:
            raise RuntimeError("files vacuumed")
        return iter([c for c in self.log if start <= c[0] <= end])


class _Info:
    def __init__(self, name):
        self.name, self.dimension, self.metric = name, 3, "cosine"


class _Collection:
    def __init__(self, backend):
        self._storage_backend = backend

    def __len__(self):
        return 0


class _DB:
    def __init__(self, backends: Dict[str, Any], make=_Backend):
        self._collections = {n: _Collection(b) for n, b in backends.items()}
        self._make = make

    def list_collections(self):
        return [_Info(n) for n in self._collections]

    def get_collection(self, name):
        return self._collections.get(name)

    def create_collection(self, name, dimension, metric=None, **kw):
        self._collections[name] = _Collection(self._make())
        return self._collections[name]


def _pair(source_backend, target_backend=None):
    source = _DB({"c": source_backend})
    target = _DB({"c": target_backend} if target_backend is not None else {})
    sync = VectrixSync(source=source, target=target, sync_documents=False)
    return sync, target


def _target_rows(target) -> Dict[str, Dict[str, Any]]:
    return target.get_collection("c")._storage_backend.rows


# ---------------------------------------------------------------------------
# Without a change feed: compare ids
# ---------------------------------------------------------------------------


def test_a_row_the_source_lacks_is_deleted_from_the_target():
    target_backend = _Backend({"keep": {"v": 1}, "gone": {"v": 2}})
    sync, target = _pair(_Backend({"keep": {"v": 1}}), target_backend)

    result = sync.cdc()

    assert result.errors == []
    assert result.rows_deleted == 1
    assert set(_target_rows(target)) == {"keep"}
    assert result.fallbacks == [], "comparing ids is the plan here, not a fallback"


def test_full_still_never_deletes():
    """cdc() is the delete-aware path; full() keeps its meaning."""
    sync, target = _pair(_Backend({"keep": {}}), _Backend({"keep": {}, "gone": {}}))

    result = sync.full()

    assert result.rows_deleted == 0
    assert set(_target_rows(target)) == {"keep", "gone"}


def test_a_delete_between_passes_reaches_the_target():
    source = _Backend({"a": {}, "b": {}})
    sync, target = _pair(source)

    sync.cdc()
    del source.rows["a"]
    result = sync.cdc()

    assert result.rows_deleted == 1
    assert set(_target_rows(target)) == {"b"}


def test_a_target_collection_is_created_when_missing():
    sync, target = _pair(_Backend({"a": {"v": 1}}))

    result = sync.cdc()

    assert result.collections_synced == ["c"]
    assert _target_rows(target) == {"a": {"v": 1}}


def test_collections_selects_what_is_synced():
    source = _DB({"c": _Backend({"a": {}}), "d": _Backend({"b": {}})})
    target = _DB({})
    result = VectrixSync(source=source, target=target).cdc(collections=["d"])

    assert result.collections_synced == ["d"]
    assert target.get_collection("c") is None


# ---------------------------------------------------------------------------
# With a change feed
# ---------------------------------------------------------------------------


def test_the_first_pass_compares_then_the_feed_is_read_from_where_it_left_off():
    source = _FeedBackend()
    source.put("a", {"v": 1})
    source.put("b", {"v": 1})
    sync, target = _pair(source, _Backend({"stale": {}}))

    first = sync.cdc()
    assert first.rows_deleted == 1, "the first pass compares, so stale rows go"
    assert source.asked == [], "there is no version to read the feed from yet"

    source.put("a", {"v": 2})
    source.remove("b")
    second = sync.cdc()

    assert source.asked == [(3, 4)]
    assert (second.rows_synced, second.rows_deleted) == (1, 1)
    assert _target_rows(target) == {"a": {"v": 2}}


def test_only_the_last_change_to_an_id_counts():
    source = _FeedBackend()
    sync, target = _pair(source)
    sync.cdc()

    source.put("a", {"v": 1})
    source.remove("a")
    source.put("b", {"v": 1})
    source.put("b", {"v": 2})
    result = sync.cdc()

    assert _target_rows(target) == {"b": {"v": 2}}
    assert (result.rows_synced, result.rows_deleted) == (1, 1)


def test_nothing_new_reads_nothing():
    source = _FeedBackend()
    source.put("a", {})
    sync, _ = _pair(source)
    sync.cdc()

    result = sync.cdc()

    assert source.asked == []
    assert (result.rows_synced, result.rows_deleted) == (0, 0)


def test_a_feed_that_cannot_answer_falls_back_and_says_so():
    source = _FeedBackend()
    source.put("a", {})
    source.put("b", {})
    sync, target = _pair(source)
    sync.cdc()

    source.remove("b")
    source.feed_broken = True
    result = sync.cdc()

    assert result.success
    assert len(result.fallbacks) == 1 and result.fallbacks[0].startswith(
        "c: change feed unavailable"
    )
    assert set(_target_rows(target)) == {"a"}

    source.feed_broken = False
    source.put("c", {})
    sync.cdc()
    assert source.asked[-1] == (4, 4), "the fallback moved the version on"


def test_a_table_made_again_is_compared_rather_than_skipped():
    """Versions start over on a new table, so head below the last version
    reached would otherwise read as nothing new, for good."""
    source = _FeedBackend()
    for i in range(3):
        source.put(f"p{i}", {})
    sync, target = _pair(source)
    sync.cdc()

    source.rows, source.log, source.version = {}, [], 0
    source.put("new", {})
    result = sync.cdc()

    assert "version went back" in result.fallbacks[0]
    assert set(_target_rows(target)) == {"new"}


def test_a_failing_collection_is_an_error_and_its_version_does_not_move():
    class Exploding(_FeedBackend):
        def insert_batch(self, collection, items):
            raise RuntimeError("target down")

    source = _FeedBackend()
    sync, _ = _pair(source, Exploding())
    source.put("a", {})

    result = sync.cdc()

    assert not result.success
    assert "target down" in result.errors[0]
    assert "c" not in sync._cdc_versions


# ---------------------------------------------------------------------------
# End to end: DeltaLakeStorage over fake_databricks
# ---------------------------------------------------------------------------


@pytest.fixture
def delta(tmp_path):
    from .test_storage_contract import _delta_fake

    storage = _delta_fake(tmp_path)
    storage.connect()
    storage.create_collection("docs", {"dimension": 3, "metric": "cosine"})
    yield storage
    storage.close()


def _memory():
    storage = InMemoryStorage(StorageConfig(backend=StorageBackend.MEMORY))
    storage.connect()
    storage.create_collection("docs", {"dimension": 3})
    return storage


def _point(text):
    return {"text_content": text, "_embedding": [0.1, 0.2, 0.3], "source": "s"}


def _ids(storage):
    return {pid for pid, _ in storage.scan("docs", limit=1000)}


def test_delta_records_its_changes(delta):
    start = delta.current_version("docs")
    delta.insert("docs", "a", _point("one"))
    delta.update("docs", "a", {"source": "t"})
    delta.delete("docs", "a")

    changes = list(delta.changes("docs", start + 1, delta.current_version("docs")))

    assert [(kind, pid) for _, kind, pid, _ in changes] == [
        ("upsert", "a"),
        ("upsert", "a"),
        ("delete", "a"),
    ]
    assert changes[0][3]["text_content"] == "one"
    assert changes[0][3]["_embedding"] == [0.1, 0.2, 0.3]
    assert changes[1][3]["source"] == "t", "the post-image, not the pre-image"


def test_a_delete_in_delta_leaves_the_target(delta):
    target = _memory()
    sync, _ = _pair(delta, target)
    sync.source._collections = {"docs": _Collection(delta)}
    sync.target._collections = {"docs": _Collection(target)}

    delta.insert("docs", "a", _point("one"))
    delta.insert("docs", "b", _point("two"))
    sync.cdc()
    assert _ids(target) == {"a", "b"}

    delta.delete("docs", "a")
    delta.insert("docs", "c", _point("three"))
    result = sync.cdc()

    assert result.fallbacks == [], "the feed was read, not the whole table"
    assert (result.rows_synced, result.rows_deleted) == (1, 1)
    assert _ids(target) == {"b", "c"}
    assert target.get("docs", "c")["text_content"] == "three"


def test_a_table_without_the_feed_falls_back_until_it_is_turned_on(delta):
    """A collection table made before this release has no feed. Reading it
    fails, the pass compares instead and names it, and enable_change_feed()
    makes the next pass read the feed."""
    table = delta._full_table_name("old")
    delta._cursor.execute(
        f"CREATE TABLE {table} (id STRING NOT NULL, data STRING, dense_embedding ARRAY<DOUBLE>,"
        " sparse_embedding STRING, late_interaction_embedding STRING, metadata STRING,"
        " text_content STRING, created_at TIMESTAMP, updated_at TIMESTAMP) USING DELTA"
    )
    delta.insert("old", "a", _point("one"))
    head = delta.current_version("old")

    with pytest.raises(Exception, match="not enabled"):
        list(delta.changes("old", 1, head))

    delta.enable_change_feed("old")
    after = delta.current_version("old")
    delta.delete("old", "a")
    assert [
        kind for _, kind, _, _ in delta.changes("old", after, delta.current_version("old"))
    ] == ["delete"]
    with pytest.raises(Exception, match="starts at version"):
        list(delta.changes("old", 1, delta.current_version("old")))


def test_the_table_name_is_bound_not_inlined():
    """table_changes takes the table as a string, so it travels as a value."""
    from .test_delta_lake_params import HOSTILE, RecordingCursor

    from vectrixdb.core.storage import DeltaLakeStorage

    storage = DeltaLakeStorage(
        StorageConfig(backend=StorageBackend.DELTA_LAKE, delta_catalog="main", delta_schema="vx")
    )
    storage._cursor = RecordingCursor()

    list(storage.changes(HOSTILE, 1, 2))

    sql, params = storage._cursor.calls[-1]
    assert HOSTILE not in sql
    assert params["table"] == storage._full_table_name(HOSTILE)
    assert (params["start"], params["end"]) == (1, 2)


# ---------------------------------------------------------------------------
# The background loop
# ---------------------------------------------------------------------------


def test_start_cdc_runs_passes_until_stopped():
    source = _Backend({"a": {}})
    sync, target = _pair(source)
    ran = threading.Event()
    original = sync.cdc

    def counting_cdc(*a, **kw):
        result = original(*a, **kw)
        ran.set()
        return result

    sync.cdc = counting_cdc  # type: ignore[method-assign]
    sync.start_cdc(interval_seconds=0.01)
    try:
        assert ran.wait(timeout=5)
    finally:
        sync.stop_cdc()

    assert sync._scheduler_thread is None
    assert set(_target_rows(target)) == {"a"}


def test_start_cdc_refuses_a_second_loop_and_a_bad_interval():
    sync, _ = _pair(_Backend())
    with pytest.raises(ValueError):
        sync.start_cdc(interval_seconds=0)

    sync.start_scheduler(interval_minutes=60)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            sync.start_cdc()
    finally:
        sync.stop_scheduler()
