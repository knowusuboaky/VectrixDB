"""Sealed shards: writing a collection past the memory one graph would need.

A sharded collection keeps one mutable head shard and seals the rest to disk
as memory-mapped views. These tests hold the four properties that make that
safe to switch on:

* it is off by default, and a collection without it is unchanged on disk,
* every operation a caller has, add, search, delete, upsert and reopen,
  gives the same answers it gives unsharded,
* a key re-added after its shard was sealed resolves to the new vector, not
  the stale copy that cannot be deleted from a read-only shard,
* compaction drops deletions and shadowed copies and packs the shards again.
"""

from __future__ import annotations

import numpy as np
import pytest

from vectrixdb.core.collection import Collection
from vectrixdb.core.types import DistanceMetric

pytest.importorskip("usearch", reason="sharding is implemented for the usearch backend")

DIMS = 8


def _vectors(count: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(size=(count, DIMS)).astype(np.float32)


def _collection(path, shard_size=None, name="c", readonly=False) -> Collection:
    return Collection(
        name,
        dimension=DIMS,
        path=path,
        metric=DistanceMetric.COSINE,
        enable_text_index=False,
        shard_size=shard_size,
        readonly=readonly,
    )


def _fill(collection: Collection, vectors: np.ndarray, prefix: str = "d") -> list:
    ids = [f"{prefix}{i}" for i in range(len(vectors))]
    collection.add(ids=ids, vectors=vectors, metadata=[{"i": i} for i in range(len(vectors))])
    return ids


class TestOffByDefault:
    def test_no_shard_size_builds_one_index(self, tmp_path):
        collection = _collection(tmp_path)
        _fill(collection, _vectors(20))
        assert not hasattr(collection._index, "shard_sizes")
        collection.close()
        assert (tmp_path / "c.usearch").exists()
        assert not list(tmp_path.glob("c.s*.usearch")), "an unsharded collection seals nothing"

    def test_a_shard_size_larger_than_the_data_writes_the_same_one_file(self, tmp_path):
        collection = _collection(tmp_path, shard_size=1000)
        _fill(collection, _vectors(20))
        collection.close()
        assert (tmp_path / "c.usearch").exists()
        assert not list(tmp_path.glob("c.s*.usearch"))

    def test_shard_size_must_be_positive(self, tmp_path):
        from vectrixdb.core.sharded_index import ShardedIndex

        with pytest.raises(ValueError):
            ShardedIndex(
                dimension=DIMS,
                metric="cos",
                connectivity=16,
                expansion_add=200,
                expansion_search=50,
                shard_size=0,
                directory=tmp_path,
                stem="x",
            )

    def test_sharding_needs_a_path(self):
        """An in-memory collection has nowhere to seal to, so it stays one
        index rather than failing."""
        collection = Collection(
            "mem", dimension=DIMS, path=None, enable_text_index=False, shard_size=2
        )
        _fill(collection, _vectors(6))
        assert collection.count() == 6
        assert not hasattr(collection._index, "shard_sizes")


class TestShardsFillAndSeal:
    def test_shards_fill_to_the_size_and_no_further(self, tmp_path):
        collection = _collection(tmp_path, shard_size=4)
        _fill(collection, _vectors(11))
        assert collection._index.shard_sizes() == [4, 4, 3]
        assert collection.count() == 11

    def test_a_sealed_shard_is_on_disk(self, tmp_path):
        collection = _collection(tmp_path, shard_size=4)
        _fill(collection, _vectors(9))
        assert sorted(p.name for p in tmp_path.glob("c.s*.usearch")) == [
            "c.s0.usearch",
            "c.s1.usearch",
        ]

    def test_one_add_larger_than_a_shard_is_split(self, tmp_path):
        collection = _collection(tmp_path, shard_size=3)
        _fill(collection, _vectors(10))
        assert collection._index.shard_sizes() == [3, 3, 3, 1]

    def test_index_size_counts_every_shard(self, tmp_path):
        collection = _collection(tmp_path, shard_size=4)
        _fill(collection, _vectors(10))
        assert collection._index_size() == 10


class TestSearchAcrossShards:
    def test_the_nearest_vector_is_found_wherever_it_sits(self, tmp_path):
        vectors = _vectors(30)
        collection = _collection(tmp_path, shard_size=4)
        _fill(collection, vectors)
        # Every document is its own nearest neighbour, including those in the
        # oldest sealed shard and those in the head.
        for target in (0, 1, 15, 29):
            found = collection.search(query=vectors[target], limit=1)
            assert found.results[0].id == f"d{target}", f"missed d{target}"

    def test_a_sharded_search_matches_an_unsharded_one(self, tmp_path):
        vectors = _vectors(40, seed=3)
        plain = _collection(tmp_path / "plain", name="p")
        _fill(plain, vectors)
        sharded = _collection(tmp_path / "sharded", name="s", shard_size=6)
        _fill(sharded, vectors)

        for query in vectors[:5]:
            want = [r.id for r in plain.search(query=query, limit=5).results]
            got = [r.id for r in sharded.search(query=query, limit=5).results]
            assert got == want

    def test_metadata_comes_back_with_a_sharded_hit(self, tmp_path):
        vectors = _vectors(12)
        collection = _collection(tmp_path, shard_size=3)
        _fill(collection, vectors)
        hit = collection.search(query=vectors[2], limit=1).results[0]
        assert hit.metadata["i"] == 2

    def test_an_empty_sharded_collection_searches_to_nothing(self, tmp_path):
        collection = _collection(tmp_path, shard_size=4)
        assert collection.search(query=_vectors(1)[0], limit=3).results == []


class TestDeleteAndUpsert:
    def test_deleting_from_a_sealed_shard_removes_it_from_results(self, tmp_path):
        vectors = _vectors(12)
        collection = _collection(tmp_path, shard_size=4)
        ids = _fill(collection, vectors)
        collection.delete([ids[1]])

        assert collection.count() == 11
        found = [r.id for r in collection.search(query=vectors[1], limit=11).results]
        assert ids[1] not in found

    def test_readding_an_id_uses_the_new_vector(self, tmp_path):
        """The old copy sits in a sealed shard and cannot be removed from it,
        so the merge has to prefer the newer one."""
        vectors = _vectors(12)
        collection = _collection(tmp_path, shard_size=4)
        ids = _fill(collection, vectors)

        collection.add(ids=[ids[0]], vectors=vectors[11:12], metadata=[{"i": "moved"}])

        assert collection.count() == 12, "an upsert is not a new document"
        near_eleven = [r.id for r in collection.search(query=vectors[11], limit=2).results]
        assert ids[0] in near_eleven, "the new vector should rank beside the one it copied"
        # The stale copy is still in its sealed shard. It must never surface
        # as a second hit for the same id.
        everything = [r.id for r in collection.search(query=vectors[0], limit=12).results]
        assert len(everything) == len(set(everything)), "an id came back twice"
        moved = collection.get(ids[0])
        assert moved is not None and moved.metadata["i"] == "moved"

    def test_delete_then_readd_brings_it_back(self, tmp_path):
        vectors = _vectors(10)
        collection = _collection(tmp_path, shard_size=3)
        ids = _fill(collection, vectors)
        collection.delete([ids[0]])
        collection.add(ids=[ids[0]], vectors=vectors[0:1], metadata=[{"i": 0}])
        found = [r.id for r in collection.search(query=vectors[0], limit=1).results]
        assert found == [ids[0]]


class TestPersistence:
    def test_a_sharded_collection_reopens_with_its_shards(self, tmp_path):
        vectors = _vectors(14)
        collection = _collection(tmp_path, shard_size=4)
        _fill(collection, vectors)
        sizes = collection._index.shard_sizes()
        collection.close()

        reopened = _collection(tmp_path, shard_size=4)
        assert reopened._index.shard_sizes() == sizes
        assert reopened.count() == 14
        assert reopened.search(query=vectors[9], limit=1).results[0].id == "d9"

    def test_an_unsharded_collection_can_be_reopened_sharded(self, tmp_path):
        """The head is the same file a single index writes, so switching the
        setting on does not strand the vectors already there."""
        vectors = _vectors(10)
        plain = _collection(tmp_path)
        _fill(plain, vectors)
        plain.close()

        sharded = _collection(tmp_path, shard_size=4)
        assert sharded.count() == 10
        assert sharded.search(query=vectors[7], limit=1).results[0].id == "d7"
        # The old vectors are in the head, so the next adds seal it.
        sharded.add(ids=["new"], vectors=_vectors(1, seed=9), metadata=[{}])
        assert sharded.count() == 11

    def test_readonly_opens_the_shards_memory_mapped(self, tmp_path):
        vectors = _vectors(12)
        collection = _collection(tmp_path, shard_size=4)
        _fill(collection, vectors)
        collection.close()

        reader = _collection(tmp_path, shard_size=4, readonly=True)
        assert reader.count() == 12
        assert reader.search(query=vectors[5], limit=1).results[0].id == "d5"


class TestCompaction:
    def test_rebuild_packs_the_shards_and_drops_deletions(self, tmp_path):
        vectors = _vectors(13)
        collection = _collection(tmp_path, shard_size=4)
        ids = _fill(collection, vectors)
        collection.delete(ids[:3])
        assert collection._index.shard_sizes() == [4, 4, 4, 1]

        live = collection.rebuild_index()

        assert live == 10
        assert sum(collection._index.shard_sizes()) == 10, "tombstones are gone"
        assert collection._index.shard_sizes() == [4, 4, 2]
        found = [r.id for r in collection.search(query=vectors[0], limit=10).results]
        assert ids[0] not in found
        assert collection.search(query=vectors[12], limit=1).results[0].id == ids[12]

    def test_compaction_leaves_no_staging_files(self, tmp_path):
        collection = _collection(tmp_path, shard_size=3)
        _fill(collection, _vectors(9))
        collection.rebuild_index()
        collection.close()
        assert not list(tmp_path.glob("*rebuild*")), "staging files are cleaned up"

    def test_a_compacted_collection_reopens(self, tmp_path):
        vectors = _vectors(11)
        collection = _collection(tmp_path, shard_size=4)
        ids = _fill(collection, vectors)
        collection.delete([ids[5]])
        collection.rebuild_index()
        collection.close()

        reopened = _collection(tmp_path, shard_size=4)
        assert reopened.count() == 10
        assert reopened.search(query=vectors[7], limit=1).results[0].id == "d7"


class TestTheIndexSurfaceDirectly:
    """The three calls Collection makes on the index object that the
    collection-level tests reach only indirectly."""

    def _index(self, tmp_path, shard_size=3):
        from vectrixdb.core.sharded_index import ShardedIndex

        index = ShardedIndex(
            dimension=DIMS,
            metric="cos",
            connectivity=16,
            expansion_add=200,
            expansion_search=50,
            shard_size=shard_size,
            directory=tmp_path,
            stem="direct",
        )
        vectors = _vectors(8)
        index.add(np.arange(8, dtype=np.uint64), vectors)
        return index, vectors

    def test_contains_spans_every_shard(self, tmp_path):
        index, _ = self._index(tmp_path)
        assert all(index.contains(key) for key in range(8))
        assert not index.contains(99)

    def test_remove_hides_a_key_in_a_sealed_shard(self, tmp_path):
        index, _ = self._index(tmp_path)
        index.remove(0)  # shard zero is sealed by now
        assert not index.contains(0)
        assert index.get(0) is None
        found = index.search(_vectors(8)[0], 8)
        assert 0 not in found.keys.tolist()

    def test_remove_from_the_head_is_immediate(self, tmp_path):
        index, _ = self._index(tmp_path)
        index.remove(7)  # the head holds the last two
        assert not index.contains(7)

    def test_get_returns_the_stored_vector(self, tmp_path):
        index, vectors = self._index(tmp_path)
        got = np.asarray(index.get(2), dtype=np.float32).reshape(-1)
        assert got.shape == (DIMS,)
        assert np.allclose(got, vectors[2], atol=1e-3)

    def test_get_of_a_missing_key_is_none(self, tmp_path):
        index, _ = self._index(tmp_path)
        assert index.get(404) is None

    def test_len_counts_every_shard(self, tmp_path):
        index, _ = self._index(tmp_path)
        assert len(index) == 8
        assert index.shard_count == 3
        assert index.files()[-1].name == "direct.usearch"

    def test_searching_an_empty_index_returns_nothing(self, tmp_path):
        from vectrixdb.core.sharded_index import ShardedIndex

        index = ShardedIndex(
            dimension=DIMS,
            metric="cos",
            connectivity=16,
            expansion_add=200,
            expansion_search=50,
            shard_size=3,
            directory=tmp_path,
            stem="empty",
        )
        found = index.search(_vectors(1)[0], 5)
        assert len(found) == 0
        assert found.keys.size == 0
