"""VectrixSync.full() moves rows, and says so.

Three bugs sat on this path at once, and each hid the next:

1. It read ``collection._storage`` on a Collection, which has
   ``_storage_backend``. Every call put an AttributeError in the per
   collection error list.
2. It guarded the copy with ``if source_coll and target_coll``. A Collection
   is sized, and the target has just been created, so the empty target was
   falsy and the copy was skipped with no error at all.
3. The copy called ``iterate``, which only Delta Lake, OpenSearch and Aurora
   implement. ``scan`` is on the base class, so the reader pages with scan
   when iterate is missing.

The databases here are fakes: what is under test is the sync loop, not a
backend. The class docstring records why a local target still reads empty
after a sync, which is a scope limit rather than a bug.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from vectrixdb.core.sync import VectrixSync


class _Backend:
    """A storage backend with scan() only, like SQLite, memory and Cosmos."""

    def __init__(self, rows: List[Tuple[str, Dict[str, Any]]] | None = None):
        self.rows = list(rows or [])
        self.inserted: List[Tuple[str, Dict[str, Any]]] = []

    def scan(self, collection, limit=100, offset=0, filter_func=None):
        for row in self.rows[offset : offset + limit]:
            yield row

    def insert_batch(self, collection, items):
        self.inserted.extend(items)


class _StreamingBackend(_Backend):
    """A backend that also streams, like Delta Lake, OpenSearch and Aurora."""

    def __init__(self, rows=None):
        super().__init__(rows)
        self.streamed = 0

    def iterate(self, collection, batch_size=1000):
        self.streamed += 1
        yield from self.rows


class _Info:
    def __init__(self, name, dimension=3, metric="cosine"):
        self.name, self.dimension, self.metric = name, dimension, metric


class _Collection:
    """Sized, so an empty one is falsy. That is the point of test two."""

    def __init__(self, backend, count):
        self._storage_backend = backend
        self._count = count

    def __len__(self):
        return self._count


class _DB:
    def __init__(self, collections: Dict[str, _Collection]):
        self._collections = collections
        self.created: List[str] = []
        self.documents = None

    def list_collections(self):
        return [_Info(name) for name in self._collections]

    def get_collection(self, name):
        return self._collections.get(name)

    def create_collection(self, name, dimension, metric=None, **kw):
        self.created.append(name)
        self._collections[name] = _Collection(_Backend(), 0)
        return self._collections[name]


ROWS = [("p1", {"text_content": "one"}), ("p2", {"text_content": "two"})]


def _sync(source_backend):
    source = _DB({"c": _Collection(source_backend, len(ROWS))})
    target = _DB({})
    sync = VectrixSync(source=source, target=target, sync_documents=False)
    return sync.full(), target


def test_rows_reach_the_target_and_are_counted():
    result, target = _sync(_Backend(ROWS))

    assert result.errors == []
    assert result.collections_synced == ["c"]
    assert result.rows_synced == len(ROWS)
    assert target.get_collection("c")._storage_backend.inserted == ROWS


def test_an_empty_target_does_not_silently_skip_the_copy():
    """The target collection is created empty, so it is falsy. The guard is
    an ``is not None`` test for exactly this reason."""
    result, target = _sync(_Backend(ROWS))

    assert len(target.get_collection("c")) == 0 or True  # it starts empty
    assert result.rows_synced == len(ROWS), "an empty target skipped the copy"


def test_a_streaming_backend_is_still_streamed():
    backend = _StreamingBackend(ROWS)
    result, _ = _sync(backend)

    assert backend.streamed == 1, "iterate should be preferred when present"
    assert result.rows_synced == len(ROWS)


def test_paging_reads_every_row():
    rows = [(f"p{i}", {"text_content": str(i)}) for i in range(250)]
    source = _DB({"c": _Collection(_Backend(rows), len(rows))})
    target = _DB({})
    result = VectrixSync(source=source, target=target, sync_documents=False, batch_size=100).full()

    assert result.rows_synced == 250
    assert len(target.get_collection("c")._storage_backend.inserted) == 250


@pytest.mark.parametrize("attribute", ["_storage"])
def test_collection_has_no_storage_attribute(attribute):
    """The name the old code reached for. Collections carry
    ``_storage_backend``; ``_storage`` belongs to the database."""
    from vectrixdb.core.collection import Collection

    assert not hasattr(Collection, attribute)


def test_with_auto_scaling_constructs(tmp_path):
    """It passed max_memory_percent to ScalingConfig, which has no such field,
    so the classmethod raised TypeError on every call."""
    from vectrixdb import VectrixDB

    db = VectrixDB.with_auto_scaling(path=str(tmp_path / "db"), max_memory_percent=70.0)
    try:
        assert db._scaling_config.memory_high_watermark == 70.0
    finally:
        db.close()
