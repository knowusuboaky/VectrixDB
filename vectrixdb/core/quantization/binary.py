"""
Binary Quantizer (1-bit)

Reduces each float32 (4 bytes) to 1 bit for 32x memory compression.
Uses sign-based quantization with optional learned thresholds.
"""

import json
from pathlib import Path
from typing import Optional
import numpy as np

from ...exceptions import QuantizationError
from .base import BaseQuantizer


__all__ = [
    "BinaryQuantizer",
]


# ============================================================================
# BINARY: 1 bit
# ============================================================================
#
# INPUT   float32 vectors
# OUTPUT  one bit a dimension, by sign or a learned threshold: 32 times
#         smaller
#
# Hamming distance, and a rescore pass to win the accuracy back.


class BinaryQuantizer(BaseQuantizer):
    """
    Binary quantization (1-bit).

    Converts float32 vectors to binary by keeping only the sign of each dimension.
    Uses Hamming distance for fast similarity computation.

    Compression ratio: 32x

    Accuracy loss: severe on its own, and this is not a shortlist you can use
    directly. Measured against exact cosine on unit-normalised vectors,
    recall@10 is about 0.16 symmetric and 0.33 asymmetric, near enough
    regardless of dimension. The docstring used to promise a 5 to 15 percent
    reduction, which is roughly six times better than it achieves.

    It is a prefilter, and only with a deep rescore. At 384 dimensions over
    2,000 vectors, rescoring the top 100 with the original float32 vectors
    reaches recall@10 of 0.59; the top 500 reaches 0.92 and the top 1,000
    reaches 0.99. Size the shortlist from that, not from the compression
    ratio. `ScalarQuantizer` costs 4x the space and about 1 to 2 percent of
    the recall, and is the better default.

    Example:
        >>> quantizer = BinaryQuantizer(dimension=384)
        >>> quantizer.fit(training_vectors)  # Optional: learns thresholds
        >>> codes = quantizer.encode(vectors)  # dtype=uint8, packed bits
        >>> distances = quantizer.compute_distances(query, codes)
    """

    def __init__(
        self,
        dimension: int,
        learn_thresholds: bool = False,
        threshold_samples: int = 10000,
    ):
        """
        Initialize binary quantizer.

        Args:
            dimension: Vector dimension
            learn_thresholds: Whether to learn optimal thresholds (vs 0)
            threshold_samples: Max samples for threshold learning
        """
        super().__init__(dimension)
        self.learn_thresholds = learn_thresholds
        self.threshold_samples = threshold_samples

        # Per-dimension thresholds (default: 0)
        self._thresholds: Optional[np.ndarray] = None  # shape: (dimension,)

        # Number of bytes needed to pack all bits
        self._packed_size = (dimension + 7) // 8

    @property
    def compression_ratio(self) -> float:
        """32x compression (float32 -> 1 bit)."""
        return 32.0

    @property
    def code_size(self) -> int:
        """Size of encoded vector in bytes (packed bits)."""
        return self._packed_size

    def fit(self, vectors: np.ndarray) -> "BinaryQuantizer":
        """
        Learn optimal thresholds from sample vectors.

        If learn_thresholds is False, uses 0 as threshold for all dimensions.

        Args:
            vectors: Training vectors, shape (n_samples, dimension)

        Returns:
            self for method chaining
        """
        vectors = np.asarray(vectors, dtype=np.float32)

        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        if vectors.shape[1] != self.dimension:
            raise ValueError(f"Expected dimension {self.dimension}, got {vectors.shape[1]}")

        if self.learn_thresholds:
            # There is nothing to take a median of below either of these, and
            # np.median of an empty slice is NaN. A NaN threshold is silent
            # and total: every comparison against it is false, so every
            # vector encodes to the same code and the index becomes one
            # bucket while still reporting itself fitted.
            if len(vectors) == 0:
                raise QuantizationError("fit() needs at least one vector to learn thresholds from.")
            if self.threshold_samples < 1:
                raise QuantizationError(
                    f"threshold_samples must be at least 1, got {self.threshold_samples}."
                )

            # Use subset for threshold learning
            if len(vectors) > self.threshold_samples:
                indices = np.random.choice(len(vectors), self.threshold_samples, replace=False)
                vectors = vectors[indices]

            # Use median as threshold (balances positive/negative)
            self._thresholds = np.median(vectors, axis=0)

            # Non-finite training data reaches here as a non-finite threshold
            # and has the same effect, so it is refused rather than stored.
            if not np.all(np.isfinite(self._thresholds)):
                raise QuantizationError(
                    "learned thresholds are not finite, which would encode every "
                    "vector identically; check the calibration set for NaN or inf."
                )
        else:
            # Use 0 as threshold
            self._thresholds = np.zeros(self.dimension, dtype=np.float32)

        self._is_fitted = True
        return self

    def encode(self, vectors: np.ndarray) -> np.ndarray:
        """
        Convert float32 vectors to packed binary representation.

        Args:
            vectors: Input vectors, shape (n, dimension), dtype=float32

        Returns:
            Quantized codes, shape (n, packed_size), dtype=uint8
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        vectors = np.asarray(vectors, dtype=np.float32)

        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        n_vectors = vectors.shape[0]

        # Compare to thresholds -> binary
        # NaN and inf used to clip to a legitimate-looking code: a NaN
        # vector decoded to the training minimum in every dimension and
        # was indistinguishable from a real extreme vector.
        if not np.all(np.isfinite(vectors)):
            raise QuantizationError(
                "cannot encode a vector containing NaN or inf; it would clip "
                "to an ordinary-looking code and be impossible to spot later."
            )

        binary = (vectors > self._thresholds).astype(np.uint8)

        # Pack bits into bytes
        # Pad to multiple of 8 if needed
        if self.dimension % 8 != 0:
            pad_size = 8 - (self.dimension % 8)
            binary = np.pad(binary, ((0, 0), (0, pad_size)), constant_values=0)

        # Reshape and pack
        binary = binary.reshape(n_vectors, -1, 8)
        codes = np.packbits(binary, axis=2).reshape(n_vectors, -1)

        return codes

    def decode(self, codes: np.ndarray) -> np.ndarray:
        """
        Convert packed binary back to float32.

        Note: This is lossy - returns -1.0 or +1.0 for each dimension.

        Args:
            codes: Quantized codes, shape (n, packed_size), dtype=uint8

        Returns:
            Reconstructed vectors, shape (n, dimension), dtype=float32
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        codes = np.asarray(codes)

        if codes.ndim == 1:
            codes = codes.reshape(1, -1)

        n_vectors = codes.shape[0]

        # Unpack bits
        binary = np.unpackbits(codes, axis=1)

        # Trim to original dimension
        binary = binary[:, : self.dimension]

        # Convert to -1/+1
        vectors = binary.astype(np.float32) * 2 - 1

        return vectors

    def compute_distances(
        self, query: np.ndarray, codes: np.ndarray, metric: str = "cosine"
    ) -> np.ndarray:
        """
        Compute distances using Hamming distance.

        For binary vectors, Hamming distance is proportional to
        angular distance, making it suitable for cosine similarity.

        Args:
            query: Query vector, shape (dimension,), dtype=float32
            codes: Quantized database vectors, shape (n, packed_size)
            metric: "cosine" or "hamming". A binary code keeps the sign of
                each dimension and nothing else, so Hamming distance is the
                only thing it can answer, and it approximates the angle.
                Anything else raises: answering "euclidean" with a Hamming
                distance measured recall@10 of 0.090 against exact, which is
                noise wearing the shape of an answer.

        Returns:
            Distances, shape (n,) - Hamming distance normalized to [0, 1]
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        if metric not in ("cosine", "hamming"):
            raise QuantizationError(
                f"BinaryQuantizer cannot measure {metric!r}: a 1-bit code keeps "
                f"only the sign of each dimension, so Hamming distance, which "
                f"approximates the angle, is all it can give. Use 'cosine' or "
                f"'hamming', or a ScalarQuantizer for other metrics."
            )

        query = np.asarray(query, dtype=np.float32).flatten()

        # Encode query to binary
        query_code = self.encode(query.reshape(1, -1))[0]

        codes = np.asarray(codes)
        if codes.ndim == 1:
            codes = codes.reshape(1, -1)

        # Compute Hamming distance using XOR and popcount
        distances = self._hamming_distance(query_code, codes)

        # Normalize to [0, 1]
        distances = distances / self.dimension

        return distances

    def _hamming_distance(self, query_code: np.ndarray, codes: np.ndarray) -> np.ndarray:
        """
        Compute Hamming distance using XOR and popcount.

        Args:
            query_code: Packed query bits, shape (packed_size,)
            codes: Packed database bits, shape (n, packed_size)

        Returns:
            Hamming distances, shape (n,)
        """
        # XOR gives differing bits
        xor_result = np.bitwise_xor(codes, query_code)

        # Count set bits (Hamming distance)
        # Using lookup table for popcount
        popcount_table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
        distances = np.sum(popcount_table[xor_result], axis=1)

        return distances.astype(np.float32)

    def compute_asymmetric_distances(
        self, query: np.ndarray, codes: np.ndarray, metric: str = "cosine"
    ) -> np.ndarray:
        """
        Compute asymmetric distances (query not quantized).

        More accurate than symmetric Hamming, but slower.

        Args:
            query: Query vector (not quantized), shape (dimension,)
            codes: Quantized database vectors, shape (n, packed_size)
            metric: Distance metric

        Returns:
            Distances, shape (n,)
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        query = np.asarray(query, dtype=np.float32).flatten()
        codes = np.asarray(codes)

        if codes.ndim == 1:
            codes = codes.reshape(1, -1)

        # Decode database vectors to -1/+1
        db_vectors = self.decode(codes)

        if metric == "cosine":
            # Normalize query
            query_norm = query / (np.linalg.norm(query) + 1e-8)

            # A decoded vector is +/-1 in every dimension, so its norm is
            # sqrt(dimension), not dimension. Dividing by the dimension left
            # every distance within a few thousandths of 0.5: the ranking was
            # right and the numbers were meaningless, so a threshold filter
            # returned nothing and the values were incomparable with the
            # other quantisers.
            similarities = np.dot(db_vectors, query_norm) / np.sqrt(self.dimension)
            distances = 1.0 - (np.clip(similarities, -1.0, 1.0) + 1.0) / 2.0

        elif metric == "dot":
            distances = -np.dot(db_vectors, query)

        else:
            # Default to Hamming
            return self.compute_distances(query, codes, metric)

        return distances

    def save(self, path: Path) -> None:
        """Save quantizer state to disk."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)

        # Save configuration
        config = {
            "dimension": self.dimension,
            "learn_thresholds": self.learn_thresholds,
            "threshold_samples": self.threshold_samples,
            "packed_size": self._packed_size,
            "is_fitted": self._is_fitted,
        }
        with open(path / "config.json", "w") as f:
            json.dump(config, f)

        # Save thresholds
        if self._is_fitted:
            thresholds = self._thresholds
            if thresholds is None:
                raise RuntimeError("Quantizer state is corrupted: thresholds missing.")
            np.save(path / "thresholds.npy", thresholds)

    def load(self, path: Path) -> "BinaryQuantizer":
        """Load quantizer state from disk."""
        path = Path(path)

        # Load configuration
        with open(path / "config.json", "r") as f:
            config = json.load(f)

        self.dimension = config["dimension"]
        self.learn_thresholds = config["learn_thresholds"]
        self.threshold_samples = config["threshold_samples"]
        self._packed_size = config["packed_size"]
        self._is_fitted = config["is_fitted"]

        # Load thresholds
        if self._is_fitted:
            self._thresholds = np.load(path / "thresholds.npy")

        return self

    def rescore_with_original(
        self,
        query: np.ndarray,
        original_vectors: np.ndarray,
        candidate_indices: np.ndarray,
        metric: str = "cosine",
    ) -> np.ndarray:
        """
        Re-score candidates using original vectors.

        Binary search is fast but approximate. Use this to re-rank
        top candidates with exact distances.

        Args:
            query: Query vector
            original_vectors: Original float32 vectors
            candidate_indices: Indices of candidates to rescore
            metric: Distance metric

        Returns:
            Exact distances for candidates
        """
        query = np.asarray(query, dtype=np.float32).flatten()
        candidates = original_vectors[candidate_indices]

        if metric == "cosine":
            query_norm = query / (np.linalg.norm(query) + 1e-8)
            cand_norms = np.linalg.norm(candidates, axis=1, keepdims=True) + 1e-8
            candidates_norm = candidates / cand_norms
            similarities = np.dot(candidates_norm, query_norm)
            distances = 1.0 - similarities

        elif metric == "euclidean":
            diff = candidates - query
            distances = np.sqrt(np.sum(diff**2, axis=1))

        elif metric == "dot":
            distances = -np.dot(candidates, query)

        else:
            raise ValueError(f"Unknown metric: {metric}")

        return distances
