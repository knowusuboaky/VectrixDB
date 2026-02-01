"""
Scalar Quantizer (8-bit)

Reduces each float32 (4 bytes) to uint8 (1 byte) for 4x memory compression.
Uses per-dimension min/max scaling for optimal precision.
"""

import json
from pathlib import Path
from typing import Optional, Tuple
import numpy as np

from .base import BaseQuantizer


class ScalarQuantizer(BaseQuantizer):
    """
    Scalar quantization (8-bit).

    Converts float32 vectors to uint8 by learning per-dimension
    min/max values and scaling to [0, 255].

    Compression ratio: 4x
    Accuracy loss: Minimal (~1-2% recall reduction)

    Example:
        >>> quantizer = ScalarQuantizer(dimension=384)
        >>> quantizer.fit(training_vectors)
        >>> codes = quantizer.encode(vectors)  # dtype=uint8
        >>> distances = quantizer.compute_distances(query, codes)
    """

    def __init__(
        self,
        dimension: int,
        calibration_size: int = 10000,
    ):
        """
        Initialize scalar quantizer.

        Args:
            dimension: Vector dimension
            calibration_size: Max samples for min/max calibration
        """
        super().__init__(dimension)
        self.calibration_size = calibration_size

        # Per-dimension calibration parameters
        self._min_vals: Optional[np.ndarray] = None  # shape: (dimension,)
        self._max_vals: Optional[np.ndarray] = None  # shape: (dimension,)
        self._scale: Optional[np.ndarray] = None     # (max - min) / 255
        self._offset: Optional[np.ndarray] = None    # min values

    @property
    def compression_ratio(self) -> float:
        """4x compression (float32 -> uint8)."""
        return 4.0

    @property
    def code_size(self) -> int:
        """Size of encoded vector in bytes."""
        return self.dimension  # 1 byte per dimension

    def fit(self, vectors: np.ndarray) -> "ScalarQuantizer":
        """
        Calibrate min/max values from sample vectors.

        Args:
            vectors: Training vectors, shape (n_samples, dimension)

        Returns:
            self for method chaining
        """
        vectors = np.asarray(vectors, dtype=np.float32)

        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        if vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Expected dimension {self.dimension}, got {vectors.shape[1]}"
            )

        # Use subset for calibration if too many vectors
        if len(vectors) > self.calibration_size:
            indices = np.random.choice(
                len(vectors), self.calibration_size, replace=False
            )
            vectors = vectors[indices]

        # Compute per-dimension min/max
        self._min_vals = np.min(vectors, axis=0)
        self._max_vals = np.max(vectors, axis=0)

        # Add small epsilon to avoid division by zero
        range_vals = self._max_vals - self._min_vals
        range_vals = np.maximum(range_vals, 1e-8)

        self._scale = range_vals / 255.0
        self._offset = self._min_vals

        self._is_fitted = True
        return self

    def encode(self, vectors: np.ndarray) -> np.ndarray:
        """
        Convert float32 vectors to uint8.

        Args:
            vectors: Input vectors, shape (n, dimension), dtype=float32

        Returns:
            Quantized codes, shape (n, dimension), dtype=uint8
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        vectors = np.asarray(vectors, dtype=np.float32)

        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        # Scale to [0, 255]
        scaled = (vectors - self._offset) / self._scale

        # Clip and convert to uint8
        codes = np.clip(scaled, 0, 255).astype(np.uint8)

        return codes

    def decode(self, codes: np.ndarray) -> np.ndarray:
        """
        Convert uint8 back to float32 (approximate).

        Args:
            codes: Quantized codes, shape (n, dimension), dtype=uint8

        Returns:
            Reconstructed vectors, shape (n, dimension), dtype=float32
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        codes = np.asarray(codes)

        if codes.ndim == 1:
            codes = codes.reshape(1, -1)

        # Reverse the scaling
        vectors = codes.astype(np.float32) * self._scale + self._offset

        return vectors

    def compute_distances(
        self,
        query: np.ndarray,
        codes: np.ndarray,
        metric: str = "cosine"
    ) -> np.ndarray:
        """
        Compute distances using asymmetric distance computation.

        The query is NOT quantized, providing better accuracy than
        symmetric quantization (where both are quantized).

        Args:
            query: Query vector, shape (dimension,), dtype=float32
            codes: Quantized database vectors, shape (n, dimension), dtype=uint8
            metric: Distance metric ("cosine", "euclidean", "dot")

        Returns:
            Distances, shape (n,)
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        query = np.asarray(query, dtype=np.float32).flatten()
        codes = np.asarray(codes)

        if codes.ndim == 1:
            codes = codes.reshape(1, -1)

        # Decode database vectors
        db_vectors = self.decode(codes)

        if metric == "cosine":
            # Normalize query
            query_norm = query / (np.linalg.norm(query) + 1e-8)

            # Normalize database vectors
            db_norms = np.linalg.norm(db_vectors, axis=1, keepdims=True) + 1e-8
            db_normalized = db_vectors / db_norms

            # Cosine similarity -> distance
            similarities = np.dot(db_normalized, query_norm)
            distances = 1.0 - similarities

        elif metric == "euclidean":
            # L2 distance
            diff = db_vectors - query
            distances = np.sqrt(np.sum(diff ** 2, axis=1))

        elif metric == "dot":
            # Negative dot product (so smaller is better)
            distances = -np.dot(db_vectors, query)

        else:
            raise ValueError(f"Unknown metric: {metric}")

        return distances

    def compute_distances_fast(
        self,
        query: np.ndarray,
        codes: np.ndarray,
        metric: str = "cosine"
    ) -> np.ndarray:
        """
        Fast distance computation using lookup tables.

        Pre-computes distance contributions for each quantization level,
        then uses table lookups instead of floating point operations.

        Args:
            query: Query vector, shape (dimension,)
            codes: Quantized database vectors, shape (n, dimension), dtype=uint8
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

        n_vectors = codes.shape[0]

        # Build lookup tables for each dimension
        # For each dimension d and quantization level q (0-255):
        # table[d, q] = contribution to distance

        if metric == "dot":
            # Pre-compute: query[d] * decoded_value[d, q]
            # decoded_value[d, q] = q * scale[d] + offset[d]
            lookup_tables = np.zeros((self.dimension, 256), dtype=np.float32)

            for d in range(self.dimension):
                for q in range(256):
                    decoded = q * self._scale[d] + self._offset[d]
                    lookup_tables[d, q] = query[d] * decoded

            # Sum contributions using table lookup
            distances = np.zeros(n_vectors, dtype=np.float32)
            for d in range(self.dimension):
                distances -= lookup_tables[d, codes[:, d]]

        else:
            # Fall back to standard computation for other metrics
            return self.compute_distances(query, codes, metric)

        return distances

    def save(self, path: Path) -> None:
        """Save quantizer state to disk."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)

        # Save configuration
        config = {
            "dimension": self.dimension,
            "calibration_size": self.calibration_size,
            "is_fitted": self._is_fitted,
        }
        with open(path / "config.json", "w") as f:
            json.dump(config, f)

        # Save calibration parameters
        if self._is_fitted:
            np.save(path / "min_vals.npy", self._min_vals)
            np.save(path / "max_vals.npy", self._max_vals)
            np.save(path / "scale.npy", self._scale)
            np.save(path / "offset.npy", self._offset)

    def load(self, path: Path) -> "ScalarQuantizer":
        """Load quantizer state from disk."""
        path = Path(path)

        # Load configuration
        with open(path / "config.json", "r") as f:
            config = json.load(f)

        self.dimension = config["dimension"]
        self.calibration_size = config["calibration_size"]
        self._is_fitted = config["is_fitted"]

        # Load calibration parameters
        if self._is_fitted:
            self._min_vals = np.load(path / "min_vals.npy")
            self._max_vals = np.load(path / "max_vals.npy")
            self._scale = np.load(path / "scale.npy")
            self._offset = np.load(path / "offset.npy")

        return self

    def get_quantization_error(self, vectors: np.ndarray) -> Tuple[float, float]:
        """
        Compute quantization error statistics.

        Args:
            vectors: Original vectors

        Returns:
            Tuple of (mean_error, max_error) as percentage of original magnitude
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        vectors = np.asarray(vectors, dtype=np.float32)
        codes = self.encode(vectors)
        reconstructed = self.decode(codes)

        # Compute relative error
        errors = np.linalg.norm(vectors - reconstructed, axis=1)
        magnitudes = np.linalg.norm(vectors, axis=1) + 1e-8
        relative_errors = errors / magnitudes

        return float(np.mean(relative_errors)), float(np.max(relative_errors))
