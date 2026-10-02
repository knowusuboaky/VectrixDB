"""Numerical accuracy tests for the quantizers.

Quantization trades precision for memory, so the only useful question is *how
much* precision it costs. The pre-existing quantization tests assert that the
classes exist; these assert bounded reconstruction error over generated inputs,
which is where quantization actually goes wrong.

Property-based cases use Hypothesis to generate the adversarial inputs that are
tedious to think of by hand: constant dimensions, huge dynamic range, values
straddling zero, single-row corpora.
"""

import numpy as np
import pytest

from vectrixdb.core.quantization import (
    BinaryQuantizer,
    ProductQuantizer,
    ScalarQuantizer,
)

hypothesis = pytest.importorskip("hypothesis", reason="hypothesis is in the dev extra")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.extra import numpy as hnp  # noqa: E402

SEED = 20260908

# Finite, non-degenerate float32 corpora.
vector_corpus = hnp.arrays(
    dtype=np.float32,
    shape=st.tuples(st.integers(min_value=8, max_value=48), st.integers(min_value=4, max_value=16)),
    elements=st.floats(
        min_value=-1e3, max_value=1e3, allow_nan=False, allow_infinity=False, width=32
    ),
)

SLOW_OK = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class TestScalarQuantizer:
    """8-bit per dimension. Error must be bounded by the quantization step."""

    @SLOW_OK
    @given(vectors=vector_corpus)
    def test_round_trip_error_is_bounded_by_the_step_size(self, vectors):
        dim = vectors.shape[1]
        q = ScalarQuantizer(dimension=dim).fit(vectors)
        restored = q.decode(q.encode(vectors))

        assert restored.shape == vectors.shape

        # Per-dimension step is (max - min) / 255. No value may be off by more
        # than one step, with a small allowance for float32 rounding.
        span = vectors.max(axis=0) - vectors.min(axis=0)
        step = np.where(span > 0, span / 255.0, 0.0)
        tolerance = step + 1e-3 * np.maximum(np.abs(vectors).max(axis=0), 1.0)

        assert np.all(np.abs(restored - vectors) <= tolerance + 1e-6)

    def test_constant_dimension_survives(self):
        """A dimension with zero variance has a zero step. It must not divide by it."""
        vectors = np.tile(np.array([[1.0, 5.0, -3.0, 0.0]], dtype=np.float32), (10, 1))
        q = ScalarQuantizer(dimension=4).fit(vectors)
        restored = q.decode(q.encode(vectors))
        assert np.all(np.isfinite(restored))
        np.testing.assert_allclose(restored, vectors, atol=1e-3)

    def test_compression_ratio_is_four(self):
        q = ScalarQuantizer(dimension=64)
        assert q.compression_ratio == pytest.approx(4.0)

    def test_encoded_codes_are_uint8(self):
        rng = np.random.default_rng(SEED)
        vectors = rng.normal(size=(32, 12)).astype(np.float32)
        codes = ScalarQuantizer(dimension=12).fit(vectors).encode(vectors)
        assert codes.dtype == np.uint8
        assert codes.shape == (32, 12)

    def test_reported_error_matches_measured_error(self):
        rng = np.random.default_rng(SEED)
        vectors = rng.normal(size=(64, 16)).astype(np.float32)
        q = ScalarQuantizer(dimension=16).fit(vectors)
        mean_error, max_error = q.get_quantization_error(vectors)

        # The method reports error *relative* to each vector's magnitude, not in
        # absolute units. Reproduce that definition exactly.
        restored = q.decode(q.encode(vectors))
        relative = np.linalg.norm(vectors - restored, axis=1) / (
            np.linalg.norm(vectors, axis=1) + 1e-8
        )
        assert mean_error == pytest.approx(float(relative.mean()), rel=1e-5, abs=1e-8)
        assert max_error == pytest.approx(float(relative.max()), rel=1e-5, abs=1e-8)
        assert 0.0 <= mean_error <= max_error
        # 8-bit scalar quantization of well-scaled data should stay well under 5%.
        assert mean_error < 0.05


class TestBinaryQuantizer:
    """1 bit per dimension. Only the sign survives, and it must survive exactly."""

    @SLOW_OK
    @given(vectors=vector_corpus)
    def test_sign_is_preserved(self, vectors):
        dim = vectors.shape[1]
        q = BinaryQuantizer(dimension=dim).fit(vectors)
        restored = q.decode(q.encode(vectors))

        assert restored.shape == vectors.shape
        # decode() is documented as returning -1.0 or +1.0 per dimension, split on
        # the quantizer's own fitted thresholds. Compare against those thresholds
        # rather than re-deriving one, which is meaningless for constant columns.
        expected_positive = vectors > q._thresholds
        assert np.array_equal(restored > 0, expected_positive)
        assert set(np.unique(restored)).issubset({-1.0, 1.0})

    def test_compression_ratio_is_32(self):
        assert BinaryQuantizer(dimension=128).compression_ratio == pytest.approx(32.0)

    def test_code_size_packs_bits(self):
        """128 dimensions must fit in 16 bytes, not 128."""
        assert BinaryQuantizer(dimension=128).code_size == 16


class TestProductQuantizer:
    """Codebook based. Error should beat storing the subvector means."""

    def test_round_trip_beats_the_trivial_baseline(self):
        rng = np.random.default_rng(SEED)
        # Clustered data, which is what PQ is designed for.
        centres = rng.normal(size=(8, 16)).astype(np.float32) * 5
        vectors = (
            np.repeat(centres, 40, axis=0) + rng.normal(size=(320, 16)).astype(np.float32) * 0.1
        )

        q = ProductQuantizer(dimension=16, n_subvectors=4, n_clusters=8, n_iterations=10)
        q.fit(vectors)
        restored = q.decode(q.encode(vectors))

        assert restored.shape == vectors.shape
        pq_error = float(np.mean((restored - vectors) ** 2))
        baseline = float(np.mean((vectors.mean(axis=0) - vectors) ** 2))
        assert pq_error < baseline, "PQ reconstruction is no better than storing the mean"

    def test_encode_is_deterministic(self):
        rng = np.random.default_rng(SEED)
        vectors = rng.normal(size=(64, 16)).astype(np.float32)
        q = ProductQuantizer(dimension=16, n_subvectors=4, n_clusters=8, n_iterations=5)
        q.fit(vectors)
        np.testing.assert_array_equal(q.encode(vectors), q.encode(vectors))

    def test_dimension_must_divide_into_subvectors(self):
        """15 does not split evenly into 4 subvectors; that must be rejected loudly."""
        with pytest.raises((ValueError, AssertionError, RuntimeError)):
            ProductQuantizer(dimension=15, n_subvectors=4).fit(
                np.random.default_rng(SEED).normal(size=(32, 15)).astype(np.float32)
            )


class TestSharedContract:
    """Behaviour every quantizer owes its caller, regardless of scheme."""

    @pytest.mark.parametrize(
        "make",
        [
            lambda d: ScalarQuantizer(dimension=d),
            lambda d: BinaryQuantizer(dimension=d),
            lambda d: ProductQuantizer(dimension=d, n_subvectors=4, n_clusters=4, n_iterations=5),
        ],
        ids=["scalar", "binary", "product"],
    )
    def test_encode_before_fit_does_not_silently_succeed(self, make):
        """Encoding without calibration must raise, not return garbage."""
        q = make(8)
        with pytest.raises(RuntimeError, match="not fitted"):
            q.encode(np.ones((4, 8), dtype=np.float32))

    @pytest.mark.parametrize(
        "make",
        [
            lambda d: ScalarQuantizer(dimension=d),
            lambda d: BinaryQuantizer(dimension=d),
        ],
        ids=["scalar", "binary"],
    )
    def test_round_trip_preserves_shape_and_finiteness(self, make):
        rng = np.random.default_rng(SEED)
        vectors = rng.normal(size=(24, 8)).astype(np.float32)
        q = make(8).fit(vectors)
        restored = q.decode(q.encode(vectors))
        assert restored.shape == vectors.shape
        assert np.all(np.isfinite(restored))
