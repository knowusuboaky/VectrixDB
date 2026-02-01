"""
Distance Functions for HNSW

Optimized distance computations using NumPy vectorization.
"""

from enum import Enum
from typing import Callable
import numpy as np


class DistanceMetric(str, Enum):
    """Supported distance metrics."""
    COSINE = "cosine"
    EUCLIDEAN = "euclidean"
    DOT = "dot"
    MANHATTAN = "manhattan"


class DistanceFunctions:
    """
    Optimized distance computations using NumPy.

    All functions return distances where smaller = more similar,
    except for dot product which is negated.
    """

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        """
        Cosine distance between two vectors.

        Returns 1 - cosine_similarity, range [0, 2].
        """
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)

        if norm_a < 1e-8 or norm_b < 1e-8:
            return 1.0

        similarity = np.dot(a, b) / (norm_a * norm_b)
        return 1.0 - similarity

    @staticmethod
    def euclidean(a: np.ndarray, b: np.ndarray) -> float:
        """
        Euclidean (L2) distance between two vectors.
        """
        return float(np.linalg.norm(a - b))

    @staticmethod
    def euclidean_squared(a: np.ndarray, b: np.ndarray) -> float:
        """
        Squared Euclidean distance (faster, avoids sqrt).
        """
        diff = a - b
        return float(np.dot(diff, diff))

    @staticmethod
    def dot(a: np.ndarray, b: np.ndarray) -> float:
        """
        Negative dot product (inner product).

        Negated so that smaller = more similar.
        """
        return -float(np.dot(a, b))

    @staticmethod
    def manhattan(a: np.ndarray, b: np.ndarray) -> float:
        """
        Manhattan (L1) distance between two vectors.
        """
        return float(np.sum(np.abs(a - b)))

    # Batch operations for efficiency
    @staticmethod
    def batch_cosine(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
        """
        Cosine distance from query to multiple vectors.

        Args:
            query: Query vector, shape (d,)
            vectors: Database vectors, shape (n, d)

        Returns:
            Distances, shape (n,)
        """
        query_norm = np.linalg.norm(query)
        if query_norm < 1e-8:
            return np.ones(len(vectors), dtype=np.float32)

        vector_norms = np.linalg.norm(vectors, axis=1)
        # Avoid division by zero
        vector_norms = np.maximum(vector_norms, 1e-8)

        similarities = np.dot(vectors, query) / (vector_norms * query_norm)
        return 1.0 - similarities

    @staticmethod
    def batch_euclidean(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
        """
        Euclidean distance from query to multiple vectors.

        Args:
            query: Query vector, shape (d,)
            vectors: Database vectors, shape (n, d)

        Returns:
            Distances, shape (n,)
        """
        diff = vectors - query
        return np.sqrt(np.sum(diff ** 2, axis=1))

    @staticmethod
    def batch_euclidean_squared(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
        """
        Squared Euclidean distance from query to multiple vectors.
        """
        diff = vectors - query
        return np.sum(diff ** 2, axis=1)

    @staticmethod
    def batch_dot(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
        """
        Negative dot product from query to multiple vectors.

        Args:
            query: Query vector, shape (d,)
            vectors: Database vectors, shape (n, d)

        Returns:
            Distances, shape (n,)
        """
        return -np.dot(vectors, query)

    @staticmethod
    def batch_manhattan(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
        """
        Manhattan distance from query to multiple vectors.
        """
        return np.sum(np.abs(vectors - query), axis=1)

    @classmethod
    def get_distance_func(cls, metric: str) -> Callable[[np.ndarray, np.ndarray], float]:
        """
        Get single-pair distance function for a metric.

        Args:
            metric: Distance metric name

        Returns:
            Distance function
        """
        funcs = {
            "cosine": cls.cosine,
            "euclidean": cls.euclidean,
            "euclidean_squared": cls.euclidean_squared,
            "dot": cls.dot,
            "manhattan": cls.manhattan,
            "l2": cls.euclidean,
            "ip": cls.dot,  # inner product
            "l1": cls.manhattan,
        }

        if metric.lower() not in funcs:
            raise ValueError(f"Unknown metric: {metric}. Available: {list(funcs.keys())}")

        return funcs[metric.lower()]

    @classmethod
    def get_batch_distance_func(cls, metric: str) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
        """
        Get batch distance function for a metric.

        Args:
            metric: Distance metric name

        Returns:
            Batch distance function
        """
        funcs = {
            "cosine": cls.batch_cosine,
            "euclidean": cls.batch_euclidean,
            "euclidean_squared": cls.batch_euclidean_squared,
            "dot": cls.batch_dot,
            "manhattan": cls.batch_manhattan,
            "l2": cls.batch_euclidean,
            "ip": cls.batch_dot,
            "l1": cls.batch_manhattan,
        }

        if metric.lower() not in funcs:
            raise ValueError(f"Unknown metric: {metric}. Available: {list(funcs.keys())}")

        return funcs[metric.lower()]


def normalize_vectors(vectors: np.ndarray) -> np.ndarray:
    """
    L2 normalize vectors.

    Args:
        vectors: Input vectors, shape (n, d) or (d,)

    Returns:
        Normalized vectors, same shape
    """
    if vectors.ndim == 1:
        norm = np.linalg.norm(vectors)
        if norm < 1e-8:
            return vectors
        return vectors / norm

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-8)
    return vectors / norms
