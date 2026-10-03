"""ProductQuantizer.fit has to run in NumPy time, and still quantise well.

Fitting 500 vectors took 96 seconds: k-means++ seeding was O(n k^2 d), the
assignment step allocated an (n, k, d) tensor, and the centroid update looped
over samples in Python. This pins both the speed and the quality after the
rewrite, because a fast quantiser that reconstructs badly is not a fix.
"""

import time

import pytest

import numpy as np

from vectrixdb.core.quantization import ProductQuantizer

DIM = 384


def _unit_vectors(n, seed=0):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal((n, DIM), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


@pytest.mark.perf
def test_fit_is_fast():
    """A loose bound on purpose: the old code needed ~96s for 500, this allows 10s for 2000."""
    vectors = _unit_vectors(2000)
    started = time.perf_counter()
    ProductQuantizer(dimension=DIM, n_subvectors=8).fit(vectors)
    elapsed = time.perf_counter() - started
    assert elapsed < 10, f"fit took {elapsed:.1f}s"


def test_reconstruction_beats_the_mean_vector():
    """The codebooks must carry real information, not just run quickly."""
    vectors = _unit_vectors(1500)
    pq = ProductQuantizer(dimension=DIM, n_subvectors=8).fit(vectors)
    codes = pq.encode(vectors)
    assert codes.shape == (1500, 8) and codes.dtype == np.uint8

    reconstructed = pq.decode(codes)
    pq_error = np.mean(np.sum((vectors - reconstructed) ** 2, axis=1))
    mean_error = np.mean(np.sum((vectors - vectors.mean(axis=0)) ** 2, axis=1))
    assert pq_error < 0.9 * mean_error, (
        f"PQ error {pq_error:.3f} vs mean-vector error {mean_error:.3f}"
    )


def test_assignment_matches_the_direct_formula():
    """The matrix-product distance must pick the same centroids as brute force."""
    rng = np.random.default_rng(3)
    sub = rng.standard_normal((300, 48), dtype=np.float32)
    centroids = rng.standard_normal((256, 48), dtype=np.float32)
    pq = ProductQuantizer(dimension=DIM, n_subvectors=8)
    fast = pq._assign_to_centroids(sub, centroids)
    direct = np.argmin(np.sum((sub[:, None, :] - centroids[None, :, :]) ** 2, axis=2), axis=1)
    assert np.array_equal(fast, direct.astype(np.uint8))
