"""A vector index that writes past RAM by sealing shards as it fills them.

usearch builds an HNSW graph by inserting one vector at a time, and every
insertion walks the graph from the top layer down, touching neighbours at
random. That walk is what makes the index fast to query and hostile to disk,
so the whole graph stays in memory while it is mutable. The consequence is a
ceiling: at 384 dimensions in float32 a vector costs about 1.9 KB of RAM
with its links, so a 16 GB machine builds roughly eight million of them
before it swaps.

This keeps the invariant, and makes the mutable graph small. A collection
becomes a list of shards. Exactly one, the head, is open for writing and
holds at most ``shard_size`` vectors. When it fills it is sealed: written to
disk and reopened as a memory-mapped view, which the operating system pages
in and out as searches touch it. A new empty head takes over. Sealed shards
never change, so nothing but the head is ever held in memory by us.

The surface here is the part of ``usearch.index.Index`` that Collection
uses, so Collection does not need to know which it has: ``add``, ``search``,
``contains``, ``remove``, ``get``, ``save`` and ``len``. Keys are the
collection's own integer ids, unique across shards, so no remapping is
needed and a search result means the same thing whichever shard answered.

Two rules make the merge correct:

* **Newest shard wins.** Re-adding an id gives it the same key, and the old
  copy sits in a sealed shard that cannot be edited. Shards are searched
  newest first and the first copy of a key seen is the one kept, so the head
  always shadows what came before it.
* **Removals are remembered, not applied.** A sealed shard is read-only, so
  a removed key is recorded and dropped at merge time instead.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

__all__ = ["ShardedIndex", "ShardedMatches"]


# ============================================================================
# SETTINGS: the shard file's name
# ============================================================================
#
# How a sealed shard is named on disk.

_SHARD_FILE = re.compile(r"\.s(\d+)\.usearch$")


# ============================================================================
# REPLACING A FILE
# ============================================================================
#
# INPUT   a temporary file and its target
# OUTPUT  os.replace, retried briefly on Windows
#
# Windows holds a file that was just closed for a moment longer than the call.


def _replace(tmp, target) -> None:
    """os.replace, retried briefly on Windows.

    Windows refuses to rename over a file another process has open without
    delete sharing, and a reader mapping a shard holds it open for a few
    milliseconds. Wait it out rather than fail a seal. This is the same rule
    Collection.save uses; it lives here too so the shard module does not
    import the collection.
    """
    delay = 0.02
    for attempt in range(8):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


# ============================================================================
# MATCHES, AND THE SHARDED INDEX
# ============================================================================
#
# INPUT   vectors, and a query with k
# OUTPUT  what search returns, shaped like usearch's Matches; one mutable head
#         shard plus any number of sealed, memory-mapped ones
#
# usearch builds an HNSW graph one insertion at a time and every insertion
# walks the graph, so a graph past RAM thrashes; sealing shards keeps the head
# small and the rest on disk.


class ShardedMatches:
    """What ``search`` returns, shaped like usearch's ``Matches``."""

    __slots__ = ("keys", "distances")

    def __init__(self, keys: np.ndarray, distances: np.ndarray) -> None:
        self.keys = keys
        self.distances = distances

    def __len__(self) -> int:
        return int(self.keys.size)


class ShardedIndex:
    """One mutable head shard plus any number of sealed, memory-mapped ones."""

    def __init__(
        self,
        *,
        dimension: int,
        metric: str,
        connectivity: int,
        expansion_add: int,
        expansion_search: int,
        shard_size: int,
        directory: Path,
        stem: str,
        readonly: bool = False,
        native_path=str,
    ) -> None:
        if shard_size < 1:
            raise ValueError(f"shard_size must be at least 1, got {shard_size}")

        self.dimension = dimension
        self.metric = metric
        self.connectivity = connectivity
        self.expansion_add = expansion_add
        self.expansion_search = expansion_search
        self.shard_size = int(shard_size)
        self.directory = Path(directory)
        self.stem = stem
        self.readonly = readonly
        self._native_path = native_path

        self._sealed: List[Any] = []
        self._sealed_paths: List[Path] = []
        # Keys removed after their shard was sealed. A sealed shard is
        # read-only, so the key is dropped when results are merged.
        self._removed: Set[int] = set()

        self._open_sealed()
        self._head = self._new_index()
        self._load_head()

    # -- construction -------------------------------------------------------

    def _new_index(self) -> Any:
        from usearch.index import Index as UsearchIndex

        return UsearchIndex(
            ndim=self.dimension,
            metric=self.metric,
            dtype="f32",
            connectivity=self.connectivity,
            expansion_add=self.expansion_add,
            expansion_search=self.expansion_search,
        )

    def _head_path(self) -> Path:
        return self.directory / f"{self.stem}.usearch"

    def _shard_path(self, number: int) -> Path:
        return self.directory / f"{self.stem}.s{number}.usearch"

    def _open_sealed(self) -> None:
        """Memory-map every sealed shard already on disk, in order."""
        found: List[Tuple[int, Path]] = []
        if self.directory.exists():
            for candidate in self.directory.glob(f"{self.stem}.s*.usearch"):
                match = _SHARD_FILE.search(candidate.name)
                if match:
                    found.append((int(match.group(1)), candidate))
        for _, path in sorted(found):
            index = self._new_index()
            if not self._view(index, path):
                continue
            self._sealed.append(index)
            self._sealed_paths.append(path)

    def _view(self, index: Any, path: Path) -> bool:
        """Memory-map ``path`` into ``index``; fall back to a read if the
        native library cannot open the path, which happens on Windows with
        long or non-ASCII paths."""
        try:
            index.view(self._native_path(path))
            return True
        except RuntimeError:
            try:
                index.load(path.read_bytes())
                return True
            except Exception:  # pragma: no cover - unreadable shard
                return False

    def _load_head(self) -> None:
        path = self._head_path()
        if not path.exists():
            return
        if self.readonly:
            self._view(self._head, path)
            return
        try:
            self._head.load(self._native_path(path))
        except RuntimeError:
            self._head.load(path.read_bytes())

    # -- writes -------------------------------------------------------------

    def add(self, keys, vectors) -> None:
        """Add or replace vectors, sealing the head whenever it fills.

        The batch is split so that no shard ever exceeds ``shard_size``.
        """
        keys_array = np.asarray(keys, dtype=np.uint64).reshape(-1)
        vector_array = np.asarray(vectors, dtype=np.float32).reshape(len(keys_array), -1)

        offset = 0
        while offset < len(keys_array):
            room = self.shard_size - len(self._head)
            if room <= 0:
                self._seal()
                room = self.shard_size
            take = min(room, len(keys_array) - offset)
            chunk_keys = keys_array[offset : offset + take]
            chunk_vectors = vector_array[offset : offset + take]

            # An id already in the head is replaced there. One in a sealed
            # shard is shadowed by this copy instead, because the merge reads
            # the newest shard first.
            for key in chunk_keys.tolist():
                try:
                    if self._head.contains(key):
                        self._head.remove(key)
                except Exception:  # pragma: no cover - backend specific
                    pass
                self._removed.discard(int(key))

            self._head.add(chunk_keys, chunk_vectors)
            offset += take

    def _seal(self) -> None:
        """Write the head to its own file and reopen it memory-mapped."""
        number = len(self._sealed)
        path = self._shard_path(number)
        tmp = path.with_suffix(".usearch.tmp")
        try:
            self._head.save(self._native_path(tmp))
        except RuntimeError:
            blob = self._head.save()
            assert blob is not None
            Path(tmp).write_bytes(bytes(blob))
        _replace(tmp, path)

        sealed = self._new_index()
        if self._view(sealed, path):
            self._sealed.append(sealed)
            self._sealed_paths.append(path)
        self._head = self._new_index()

    def release(self) -> None:
        """Drop every sealed mapping.

        A memory-mapped file cannot be renamed or deleted on Windows while
        the mapping is open, so compaction releases before it moves files.
        Dropping the last reference unmaps it.
        """
        self._sealed = []
        self._sealed_paths = []

    def remove(self, key) -> None:
        key = int(key)
        try:
            if self._head.contains(key):
                self._head.remove(key)
        except Exception:  # pragma: no cover - backend specific
            pass
        # A key re-added after its shard was sealed also has a sealed copy,
        # which returning early here used to bring back from the dead.
        if any(self._contains(shard, key) for shard in self._sealed):
            self._removed.add(key)

    def contains(self, key) -> bool:
        key = int(key)
        if key in self._removed:
            return False
        try:
            if self._head.contains(key):
                return True
        except Exception:  # pragma: no cover - backend specific
            pass
        return any(self._contains(shard, key) for shard in self._sealed)

    @staticmethod
    def _contains(shard: Any, key: int) -> bool:
        try:
            return bool(shard.contains(key))
        except Exception:  # pragma: no cover - backend specific
            return False

    @classmethod
    def _contains_many(cls, shard: Any, keys: np.ndarray) -> np.ndarray:
        """Whether the shard holds each key, in one call where the backend allows it."""
        try:
            return np.asarray(shard.contains(keys), dtype=bool).reshape(-1)
        except Exception:  # pragma: no cover - backend specific
            return np.array([cls._contains(shard, int(key)) for key in keys], dtype=bool)

    def get(self, key):
        """The vector for ``key``, from the newest shard that holds it."""
        key = int(key)
        if key in self._removed:
            return None
        for shard in [self._head] + list(reversed(self._sealed)):
            try:
                if shard.contains(key):
                    return shard.get(key)
            except Exception:  # pragma: no cover - backend specific
                continue
        return None

    # -- reads --------------------------------------------------------------

    def search(self, query, count: int) -> ShardedMatches:
        """Search every shard and merge by distance.

        Each shard is asked for ``count`` neighbours, because any one of them
        could hold all of the nearest. Shards are read newest first so a key
        that was re-added shadows its older copy.
        """
        count = max(1, int(count))
        shards = [self._head] + list(reversed(self._sealed))

        # Every shard's candidates, merged by distance, in arrays: with
        # tombstones the collection asks for thousands, and a Python loop over
        # each of them doubled the time of a search across fifty shards.
        found_keys: List[np.ndarray] = []
        found_distances: List[np.ndarray] = []
        found_orders: List[np.ndarray] = []
        for order, shard in enumerate(shards):
            size = len(shard)
            if size == 0:
                continue
            try:
                matches = shard.search(query, min(count, size))
            except Exception:  # pragma: no cover - backend specific
                continue
            keys = getattr(matches, "keys", None)
            if keys is None:
                continue
            keys = np.asarray(keys, dtype=np.uint64).reshape(-1)
            found_keys.append(keys)
            found_distances.append(
                np.asarray(matches.distances, dtype=np.float64).reshape(-1)[: len(keys)]
            )
            found_orders.append(np.full(len(keys), order))
        if not found_keys:
            return self._matches({}, count)
        held = np.concatenate(found_keys)
        distances = np.concatenate(found_distances)
        orders = np.concatenate(found_orders)

        # A key a newer shard holds is shadowed here, even when that newer
        # copy was too far away to be among its shard's results: taking this
        # stale copy returned a vector the key no longer has. Candidates sit
        # in shard order, so each shard is asked once, for the slice of
        # candidates that came from the shards older than it.
        stale = np.zeros(len(held), dtype=bool)
        for newer, shard in enumerate(shards[:-1]):
            start = int(np.searchsorted(orders, newer, side="right"))
            if len(shard) and start < len(held):
                stale[start:] |= self._contains_many(shard, held[start:])
        ranked = np.argsort(distances, kind="stable")
        held, distances, stale = held[ranked], distances[ranked], stale[ranked]
        shadowed = bool(stale.any())
        live = ~stale
        if self._removed:
            live &= ~np.isin(
                held, np.fromiter(self._removed, dtype=np.uint64, count=len(self._removed))
            )
        held, distances = held[live], distances[live]
        # The nearest copy of a key that is live in more than one place.
        _unique, first = np.unique(held, return_index=True)
        first.sort()
        first = first[:count]
        best: Dict[int, float] = dict(zip(held[first].tolist(), distances[first].tolist()))

        if shadowed and len(best) < count:
            # Shadowed copies crowded out live keys: rare, so the thorough
            # walk that asks each shard for more is kept for it alone.
            return self._search_past_shadows(query, count, shards)
        return self._matches(best, count)

    def _search_past_shadows(self, query, count: int, shards: List[Any]) -> ShardedMatches:
        """The merge, asking a shard again for more when shadowed keys fill its answer."""
        best: Dict[int, float] = {}
        newer: List[Any] = []
        for shard in shards:
            size = len(shard)
            if size == 0:
                newer.append(shard)
                continue
            asked = min(count, size)
            found: Dict[int, float] = {}
            while True:
                try:
                    matches = shard.search(query, asked)
                except Exception:  # pragma: no cover - backend specific
                    break
                keys = getattr(matches, "keys", None)
                if keys is None:
                    break
                found = {}
                skipped = 0
                for key, distance in zip(
                    np.asarray(keys).flatten().tolist(),
                    np.asarray(matches.distances).flatten().tolist(),
                ):
                    key = int(key)
                    if key in found:
                        continue
                    if (
                        key in best
                        or key in self._removed
                        or any(self._contains(n, key) for n in newer)
                    ):
                        skipped += 1
                        continue  # an older copy, or a removed key
                    found[key] = float(distance)
                if not skipped or len(found) >= count or asked >= size:
                    break
                asked = min(size, asked * 2)
            best.update(found)
            newer.append(shard)
        return self._matches(best, count)

    @staticmethod
    def _matches(best: Dict[int, float], count: int) -> ShardedMatches:
        if not best:
            return ShardedMatches(np.zeros(0, dtype=np.uint64), np.zeros(0, dtype=np.float32))
        ordered = sorted(best.items(), key=lambda item: item[1])[:count]
        return ShardedMatches(
            np.array([k for k, _ in ordered], dtype=np.uint64),
            np.array([d for _, d in ordered], dtype=np.float32),
        )

    def __len__(self) -> int:
        """Keys held across every shard, tombstones and shadowed copies
        included. The live count is the collection's."""
        return len(self._head) + sum(len(shard) for shard in self._sealed)

    # -- persistence --------------------------------------------------------

    def save(self, path=None):
        """Persist the head. Sealed shards were written when they were sealed.

        Collection.save() calls this with the path it wants the head at,
        which is the same file an unsharded collection uses, so a collection
        that never filled a shard is byte for byte what it always was.
        """
        if path is None:
            return self._head.save()
        return self._head.save(path)

    def load(self, source) -> None:  # pragma: no cover - Collection loads at open
        self._head.load(source)

    def view(self, source) -> None:  # pragma: no cover - Collection views at open
        self._head.view(source)

    # -- maintenance --------------------------------------------------------

    @property
    def shard_count(self) -> int:
        """Shards in total, the head included."""
        return len(self._sealed) + 1

    def shard_sizes(self) -> List[int]:
        return [len(shard) for shard in self._sealed] + [len(self._head)]

    def files(self) -> List[Path]:
        """Every file this index owns, sealed shards first."""
        return list(self._sealed_paths) + [self._head_path()]

    def rebuilt(self, keys: Sequence[int], vectors: Iterable[np.ndarray]) -> "ShardedIndex":
        """A fresh index over ``keys``, with the shard files rewritten.

        This is compaction: shadowed copies and removed keys are gone because
        only the live vectors are added, and the shards are packed full
        again.

        The new shards are written under a staging name first. The old files
        are memory-mapped, and a mapped file cannot be renamed or deleted on
        Windows, so nothing touches them until every mapping is released. A
        run that dies part way leaves the old shards intact and a staging
        file behind, which the next compaction clears.
        """
        staging = f"{self.stem}.rebuild"
        for stale in self.directory.glob(f"{staging}*.usearch"):
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - held by something else
                pass

        fresh = ShardedIndex(
            dimension=self.dimension,
            metric=self.metric,
            connectivity=self.connectivity,
            expansion_add=self.expansion_add,
            expansion_search=self.expansion_search,
            shard_size=self.shard_size,
            directory=self.directory,
            stem=staging,
            readonly=False,
            native_path=self._native_path,
        )
        rows = [np.asarray(v, dtype=np.float32).reshape(-1) for v in vectors]
        if rows:
            fresh.add(np.asarray(keys, dtype=np.uint64), np.vstack(rows))

        old_paths = list(self._sealed_paths)
        self.release()
        for path in old_paths:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - held by another reader
                pass
        fresh._rename_to(self.stem)
        return fresh

    def _rename_to(self, stem: str) -> None:
        """Move this index's sealed files to ``stem``'s names and re-map them.

        The head is not moved: Collection writes it through ``save`` right
        after, to the path the new stem implies.
        """
        staged = list(self._sealed_paths)
        self.release()
        self.stem = stem
        for number, source in enumerate(staged):
            target = self._shard_path(number)
            try:
                _replace(source, target)
            except OSError:  # pragma: no cover - staging file vanished
                continue
            index = self._new_index()
            if self._view(index, target):
                self._sealed.append(index)
                self._sealed_paths.append(target)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        sizes = self.shard_sizes()
        return (
            f"ShardedIndex(shards={len(sizes)}, sizes={sizes}, "
            f"shard_size={self.shard_size}, removed={len(self._removed)})"
        )
