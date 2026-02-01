"""
Base Quantizer Abstract Class

Defines the interface for all vector quantizers in VectrixDB.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any
import numpy as np


class QuantizationType(str, Enum):
    """Supported quantization types."""
    NONE = "none"
    SCALAR = "scalar"      # 8-bit scalar quantization
    BINARY = "binary"      # 1-bit binary quantization
    PRODUCT = "product"    # Product quantization


@dataclass
class QuantizationConfig:
    """Configuration for vector quantization."""
    type: QuantizationType = QuantizationType.NONE

    # Scalar quantization settings
    scalar_bits: int = 8  # Only 8-bit supported currently
    scalar_calibration_size: int = 10000

    # Binary quantization settings
    binary_threshold_learning: bool = False
    binary_threshold_samples: int = 10000

    # Product quantization settings
    pq_subvectors: int = 8
    pq_clusters: int = 256
    pq_train_size: int = 50000
    pq_iterations: int = 20

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "type": self.type.value,
            "scalar_bits": self.scalar_bits,
            "scalar_calibration_size": self.scalar_calibration_size,
            "binary_threshold_learning": self.binary_threshold_learning,
            "binary_threshold_samples": self.binary_threshold_samples,
            "pq_subvectors": self.pq_subvectors,
            "pq_clusters": self.pq_clusters,
            "pq_train_size": self.pq_train_size,
            "pq_iterations": self.pq_iterations,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QuantizationConfig":
        """Create from dictionary."""
        return cls(
            type=QuantizationType(data.get("type", "none")),
            scalar_bits=data.get("scalar_bits", 8),
            scalar_calibration_size=data.get("scalar_calibration_size", 10000),
            binary_threshold_learning=data.get("binary_threshold_learning", False),
            binary_threshold_samples=data.get("binary_threshold_samples", 10000),
            pq_subvectors=data.get("pq_subvectors", 8),
            pq_clusters=data.get("pq_clusters", 256),
            pq_train_size=data.get("pq_train_size", 50000),
            pq_iterations=data.get("pq_iterations", 20),
        )


class BaseQuantizer(ABC):
    """
    Abstract base class for vector quantizers.

    All quantizers must implement:
    - fit(): Train the quantizer on sample vectors
    - encode(): Convert float vectors to quantized representation
    - decode(): Reconstruct vectors from quantized codes (approximate)
    - compute_distances(): Compute distances using asymmetric distance computation
    """

    def __init__(self, dimension: int):
        """
        Initialize quantizer.

        Args:
            dimension: Vector dimension
        """
        self.dimension = dimension
        self._is_fitted = False

    @property
    def is_fitted(self) -> bool:
        """Whether the quantizer has been trained."""
        return self._is_fitted

    @property
    @abstractmethod
    def compression_ratio(self) -> float:
        """
        Memory compression ratio.

        For example:
        - Scalar (8-bit): 4.0 (float32 -> uint8)
        - Binary (1-bit): 32.0 (float32 -> 1 bit)
        """
        pass

    @property
    @abstractmethod
    def code_size(self) -> int:
        """Size of encoded vector in bytes."""
        pass

    @abstractmethod
    def fit(self, vectors: np.ndarray) -> "BaseQuantizer":
        """
        Train the quantizer on sample vectors.

        Args:
            vectors: Training vectors, shape (n_samples, dimension)

        Returns:
            self for method chaining
        """
        pass

    @abstractmethod
    def encode(self, vectors: np.ndarray) -> np.ndarray:
        """
        Encode vectors to quantized representation.

        Args:
            vectors: Input vectors, shape (n, dimension), dtype=float32

        Returns:
            Quantized codes (dtype and shape depend on quantizer type)
        """
        pass

    @abstractmethod
    def decode(self, codes: np.ndarray) -> np.ndarray:
        """
        Decode quantized codes back to vectors (approximate).

        Args:
            codes: Quantized codes from encode()

        Returns:
            Reconstructed vectors, shape (n, dimension), dtype=float32
        """
        pass

    @abstractmethod
    def compute_distances(
        self,
        query: np.ndarray,
        codes: np.ndarray,
        metric: str = "cosine"
    ) -> np.ndarray:
        """
        Compute distances between query and quantized vectors.

        Uses asymmetric distance computation (ADC) where query is not quantized
        but database vectors are. This provides better accuracy than symmetric.

        Args:
            query: Query vector, shape (dimension,)
            codes: Quantized database vectors from encode()
            metric: Distance metric ("cosine", "euclidean", "dot")

        Returns:
            Distances, shape (n,)
        """
        pass

    @abstractmethod
    def save(self, path: Path) -> None:
        """
        Save quantizer state to disk.

        Args:
            path: Directory to save to
        """
        pass

    @abstractmethod
    def load(self, path: Path) -> "BaseQuantizer":
        """
        Load quantizer state from disk.

        Args:
            path: Directory to load from

        Returns:
            self for method chaining
        """
        pass

    def fit_encode(self, vectors: np.ndarray) -> np.ndarray:
        """
        Fit quantizer and encode vectors in one step.

        Args:
            vectors: Vectors to fit on and encode

        Returns:
            Quantized codes
        """
        self.fit(vectors)
        return self.encode(vectors)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"dimension={self.dimension}, "
            f"compression_ratio={self.compression_ratio:.1f}x, "
            f"fitted={self._is_fitted})"
        )
