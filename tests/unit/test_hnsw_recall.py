"""Correctness tests for the pure-Python HNSW index.

The pre-existing HNSW tests assert only that the class exists. These assert that
the index actually finds nearest neighbours, measured against brute-force ground
truth, which is the only thing that makes an approximate index trustworthy.

Sizes here are small on purpose. `NativeHNSWIndex` currently costs roughly
O(graph) distance computations per insertion, so a 1,000-vector build does not
finish in a reasonable time; see `test_build_cost_ceiling` for the measured
ceiling and `docs/explanation/hnsw.md` for the analysis. Keeping the suite fast
matters more than exercising a size the implementation cannot serve yet.
"""

import time

import numpy as np
import pytest

from vectrixdb.core.hnsw import NativeHNSWIndex

SEED = 20260908


def _corpus(n: int, dim: int, seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(size=(n, dim)).astype(np.float32)


def _brute_force(vectors: np.ndarray, query: np.ndarray, k: int, metric: str) -> set:
    """Exact top-k indices, computed directly. This is the ground truth."""
    if metric == "cosine":
        v = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        scores = v @ (query / np.linalg.norm(query))
        order = np.argsort(-scores)
    elif metric == "euclidean":
        order = np.argsort(np.linalg.norm(vectors - query, axis=1))
    elif metric == "dot":
        order = np.argsort(-(vectors @ query))
    else:
        raise ValueError(f"unhandled metric {metric!r}")
    return set(order[:k].tolist())


def _build(vectors: np.ndarray, metric: str = "cosine", **kw) -> NativeHNSWIndex:
    index = NativeHNSWIndex(
        dimension=vectors.shape[1],
        metric=metric,
        max_elements=max(len(vectors), 16),
        seed=SEED,
        **kw,
    )
    index.add(vectors)
    return index


class TestRecall:
    """Recall against exact search: the property that actually matters."""

    @pytest.mark.parametrize("metric", ["cosine", "euclidean"])
    def test_recall_at_10(self, metric):
        vectors = _corpus(60, 16)
        index = _build(vectors, metric=metric)
        rng = np.random.default_rng(SEED + 1)
        queries = rng.normal(size=(15, 16)).astype(np.float32)

        k = 10
        hits = sum(
            len(
                _brute_force(vectors, q, k, metric)
                & {int(i) for i in index.search(q, k=k, ef=64)[0]}
            )
            for q in queries
        )
        recall = hits / (len(queries) * k)
        # A correct graph and distance function saturate at this size. A broken
        # metric, a mis-wired layer, or an off-by-one in neighbour selection does not.
        assert recall >= 0.95, f"recall@{k} for {metric} was {recall:.3f}"


class TestSearchContract:
    """Invariants every caller is entitled to rely on."""

    def test_returns_k_results(self):
        index = _build(_corpus(40, 16))
        ids, distances = index.search(_corpus(40, 16)[0], k=7)
        assert len(ids) == 7
        assert len(distances) == 7

    def test_distances_are_sorted_ascending(self):
        vectors = _corpus(40, 16)
        index = _build(vectors)
        rng = np.random.default_rng(SEED + 3)
        for _ in range(5):
            _, distances = index.search(rng.normal(size=16).astype(np.float32), k=10)
            assert np.all(np.diff(distances) >= -1e-6), "results not ordered by distance"

    def test_exact_match_ranks_first(self):
        """Querying with a stored vector must return that vector first."""
        vectors = _corpus(40, 16)
        index = _build(vectors)
        for probe in (0, 17, 39):
            ids, distances = index.search(vectors[probe], k=1)
            assert int(ids[0]) == probe
            assert distances[0] == pytest.approx(0.0, abs=1e-4)

    def test_k_larger_than_corpus_is_clamped(self):
        index = _build(_corpus(5, 8))
        ids, _ = index.search(_corpus(5, 8)[0], k=50)
        assert len(ids) <= 5

    def test_ids_are_unique(self):
        vectors = _corpus(40, 16)
        index = _build(vectors)
        rng = np.random.default_rng(SEED + 4)
        ids, _ = index.search(rng.normal(size=16).astype(np.float32), k=15)
        assert len({int(i) for i in ids}) == len(ids), "duplicate ids returned"

    def test_empty_index_returns_nothing(self):
        index = NativeHNSWIndex(dimension=8, max_elements=16, seed=SEED)
        ids, _ = index.search(np.zeros(8, dtype=np.float32), k=5)
        assert len(ids) == 0


class TestMutation:
    def test_count_tracks_adds(self):
        index = NativeHNSWIndex(dimension=8, max_elements=64, seed=SEED)
        assert index.count == 0
        index.add(_corpus(10, 8))
        assert index.count == 10
        index.add(_corpus(5, 8, seed=SEED + 9))
        assert index.count == 15

    def test_grows_past_initial_capacity(self):
        """max_elements is documented as an initial capacity, not a hard limit."""
        index = NativeHNSWIndex(dimension=8, max_elements=4, seed=SEED)
        index.add(_corpus(30, 8))
        assert index.count == 30

    def test_removed_vectors_are_not_returned(self):
        vectors = _corpus(40, 8)
        index = NativeHNSWIndex(dimension=8, max_elements=64, seed=SEED)
        index.add(vectors, ids=[f"v{i}" for i in range(40)])

        assert index.remove(["v0"]) == 1

        ids, _ = index.search(vectors[0], k=5)
        assert 0 not in [int(i) for i in ids], "removed vector still returned by search"


class TestPersistence:
    def test_save_load_round_trip_preserves_results(self, tmp_path):
        vectors = _corpus(40, 16)
        index = _build(vectors)
        query = vectors[7]
        before_ids, before_dist = index.search(query, k=10)

        path = tmp_path / "index.hnsw"
        index.save(path)

        reloaded = NativeHNSWIndex(dimension=16, max_elements=64, seed=SEED).load(path)
        after_ids, after_dist = reloaded.search(query, k=10)

        assert [int(i) for i in after_ids] == [int(i) for i in before_ids]
        np.testing.assert_allclose(after_dist, before_dist, rtol=1e-5, atol=1e-6)


@pytest.mark.slow
def test_build_cost_ceiling():
    """Pin the known performance ceiling so a fix, or a regression, is visible.

    Measured on the reference machine at the time of writing: 20 vectors build in
    ~0.01s, 40 in ~2.7s, 80 in ~15s. Insertion cost grows far faster than linear
    because `_search_layer` and `_select_neighbors_heuristic` call a scalar Python
    distance function once per candidate pair rather than batching with NumPy.

    This test does not assert the index is fast. It asserts the ceiling has not
    moved further in the wrong direction, and it will start failing loudly, in a
    good way, once the distance loop is vectorised and the bound can be tightened.
    """
    vectors = _corpus(40, 16)
    started = time.perf_counter()
    _build(vectors)
    elapsed = time.perf_counter() - started
    assert elapsed < 20.0, (
        f"building a 40-vector index took {elapsed:.1f}s. "
        "See docs/explanation/hnsw.md: insertion is not vectorised."
    )
