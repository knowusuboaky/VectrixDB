"""NativeHNSWIndex has to be usable at a size that is not a toy.

Build cost grew close to cubically: 0.5s for 40 vectors, 18s for 160, 160s for
320 on the reference machine, because every inner loop called a scalar distance
function once per pair. These pin two things after the rewrite: the index still
finds the right neighbours, and it does so in a time that permits using it.
"""

import time

import numpy as np
import pytest

from vectrixdb.core.hnsw import NativeHNSWIndex


def _brute_force(vectors: np.ndarray, query: np.ndarray, k: int, metric: str) -> set:
    if metric == "cosine":
        v = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        q = query / np.linalg.norm(query)
        dist = 1 - v @ q
    else:
        dist = np.linalg.norm(vectors - query, axis=1)
    return set(np.argsort(dist)[:k].tolist())


@pytest.mark.parametrize("metric", ["cosine", "euclidean"])
def test_recall_against_brute_force(metric):
    rng = np.random.default_rng(7)
    n, dim, k = 500, 32, 10
    vectors = rng.random((n, dim), dtype=np.float32)
    index = NativeHNSWIndex(dimension=dim, metric=metric, ef_construction=100, ef_search=100)
    index.add(vectors, [str(i) for i in range(n)])

    hits = 0
    queries = 25
    for i in range(queries):
        query = vectors[rng.integers(n)] + rng.normal(0, 0.05, dim).astype(np.float32)
        ids, _ = index.search(query, k=k)
        hits += len(set(ids.tolist()) & _brute_force(vectors, query, k, metric))
    recall = hits / (queries * k)
    assert recall >= 0.9, f"recall@{k} = {recall:.2f} on {metric}"


def test_search_returns_sorted_distances():
    rng = np.random.default_rng(1)
    vectors = rng.random((200, 16), dtype=np.float32)
    index = NativeHNSWIndex(dimension=16)
    index.add(vectors)
    _, distances = index.search(vectors[3], k=8)
    assert list(distances) == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-5), "the vector itself is its nearest"


@pytest.mark.perf
@pytest.mark.slow
def test_build_time_is_no_longer_cubic():
    """A loose bound on purpose. 320 vectors took 160s before; this allows 15.

    CI machines vary, so this is not a benchmark. It exists so a change that
    brings the per-pair loops back cannot pass unnoticed.
    """
    rng = np.random.default_rng(3)
    vectors = rng.random((320, 64), dtype=np.float32)
    index = NativeHNSWIndex(dimension=64)
    started = time.perf_counter()
    index.add(vectors)
    elapsed = time.perf_counter() - started
    assert elapsed < 15, f"building 320 vectors took {elapsed:.1f}s"


def test_deleted_vectors_are_not_returned():
    rng = np.random.default_rng(5)
    vectors = rng.random((60, 8), dtype=np.float32)
    index = NativeHNSWIndex(dimension=8)
    index.add(vectors, [str(i) for i in range(60)])
    index.remove(["0", "1", "2"])
    ids, _ = index.search(vectors[0], k=5)
    assert not {0, 1, 2} & set(ids.tolist())
