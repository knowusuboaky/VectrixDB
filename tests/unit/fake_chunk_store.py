"""A chunk store in a dict, for the tests of everything that uses one.

It is a second implementation of :class:`vectrixdb.chunk_store.CollectionChunks`
beside the Cosmos one, written from the protocol alone, so a test that passes
against it passes for any store that keeps the protocol and not only for
Cosmos. One instance is what every process of a deployment would share: hand
it to two databases on two paths and a write through one is a row the other
reads.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from vectrixdb._time import parse_iso, utcnow
from vectrixdb.core.types import Point


class MemoryChunks:
    """Every collection's rows, keyed by collection and point id."""

    def __init__(self) -> None:
        self.rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._clock = itertools.count(1_800_000_000)

    def collection(self, name: str) -> "_MemoryCollection":
        return _MemoryCollection(self, name)

    def describe(self) -> str:
        return "memory"

    def of(self, name: str) -> Dict[str, Dict[str, Any]]:
        """One collection's rows by point id, for a test to look at."""
        return {point: row for (collection, point), row in self.rows.items() if collection == name}


class _MemoryCollection:
    def __init__(self, store: MemoryChunks, name: str) -> None:
        self.store = store
        self.name = name

    def _mine(self) -> List[Tuple[str, Dict[str, Any]]]:
        return sorted(
            (point, row)
            for (collection, point), row in self.store.rows.items()
            if collection == self.name
        )

    def put(self, ids, texts, metadata, written) -> None:
        for point, text, meta in zip(ids, texts, metadata):
            held = self.store.rows.get((self.name, point))
            self.store.rows[(self.name, point)] = {
                "text": text or "",
                "metadata": dict(meta or {}),
                "written": held["written"] if held else written,
                "changed": next(self.store._clock),
            }

    def delete(self, ids) -> int:
        gone = 0
        for point in dict.fromkeys(ids):
            if self.store.rows.pop((self.name, point), None) is not None:
                gone += 1
        return gone

    def update(self, id, metadata, merge=True) -> bool:
        row = self.store.rows.get((self.name, id))
        if row is None:
            return False
        row["metadata"] = {**row["metadata"], **dict(metadata)} if merge else dict(metadata)
        row["changed"] = next(self.store._clock)
        return True

    def clear(self) -> int:
        mine = [point for point, _ in self._mine()]
        for point in mine:
            del self.store.rows[(self.name, point)]
        return len(mine)

    def count(self) -> int:
        return len(self._mine())

    def changed_at(self) -> Optional[str]:
        stamps = [row["changed"] for _, row in self._mine()]
        return datetime.fromtimestamp(max(stamps), timezone.utc).isoformat() if stamps else None

    def written(self):
        for _, row in self._mine():
            meta = row["metadata"]
            yield (
                row["written"],
                {k: meta[k] for k in ("_vx_build", "_vx_quality") if meta.get(k) is not None},
            )

    def scores(self):
        for point, row in self._mine():
            quality = row["metadata"].get("_vx_quality")
            yield point, float(quality) if isinstance(quality, (int, float)) else None

    def get(self, id) -> Optional[Point]:
        row = self.store.rows.get((self.name, id))
        if row is None:
            return None
        return Point(
            id=id,
            vector=[],
            metadata=dict(row["metadata"]),
            text=row["text"],
            created_at=parse_iso(row["written"]) or utcnow(),
        )

    def ids(self, limit, offset) -> List[str]:
        return [point for point, _ in self._mine()][offset : offset + limit]

    def each(self):
        for point, row in self._mine():
            yield point, dict(row["metadata"])

    def of_document(self, doc_id) -> List[str]:
        return [point for point, row in self._mine() if row["metadata"].get("_vx_doc") == doc_id]


class RefusingChunks(MemoryChunks):
    """A store that is down: every write raises, the way an unreachable account does."""

    def collection(self, name: str) -> "_Refusing":
        return _Refusing(self, name)


class _Refusing(_MemoryCollection):
    def put(self, *args, **kwargs):
        raise ConnectionError("the chunk store did not answer")

    def delete(self, *args, **kwargs):
        raise ConnectionError("the chunk store did not answer")

    def update(self, *args, **kwargs):
        raise ConnectionError("the chunk store did not answer")

    def clear(self, *args, **kwargs):
        raise ConnectionError("the chunk store did not answer")
