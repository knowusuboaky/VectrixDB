"""
Product Quantizer (PQ)

Splits vectors into subvectors and quantizes each to a codebook entry.
Provides configurable compression with good accuracy retention.
"""

import json
from pathlib import Path
from typing import Optional, List, Tuple
import numpy as np

from .base import BaseQuantizer


class ProductQuantizer(BaseQuantizer):
    """
    Product Quantization for high compression.

    Splits each vector into M subvectors, then quantizes each subvector
    to the nearest entry in a learned codebook of K centroids.

    Compression ratio: (dimension * 4) / M bytes
    - M=8, dim=384: 384*4/8 = 192x compression (code size = 8 bytes)
    - M=16, dim=384: 384*4/16 = 96x compression (code size = 16 bytes)

    Example:
        >>> quantizer = ProductQuantizer(dimension=384, n_subvectors=8)
        >>> quantizer.fit(training_vectors)
        >>> codes = quantizer.encode(vectors)  # shape (n, 8), dtype=uint8
        >>> distances = quantizer.compute_distances(query, codes)
    """

    def __init__(
        self,
        dimension: int,
        n_subvectors: int = 8,
        n_clusters: int = 256,
        n_iterations: int = 20,
        train_size: int = 50000,
    ):
        """
        Initialize product quantizer.

        Args:
            dimension: Vector dimension (must be divisible by n_subvectors)
            n_subvectors: Number of subvector segments (M)
            n_clusters: Codebook size per subvector (K), max 256 for uint8
            n_iterations: K-means iterations for codebook training
            train_size: Max samples for codebook training
        """
        super().__init__(dimension)

        if dimension % n_subvectors != 0:
            raise ValueError(
                f"Dimension {dimension} must be divisible by n_subvectors {n_subvectors}"
            )

        if n_clusters > 256:
            raise ValueError(
                f"n_clusters must be <= 256 for uint8 codes, got {n_clusters}"
            )

        self.n_subvectors = n_subvectors
        self.n_clusters = n_clusters
        self.n_iterations = n_iterations
        self.train_size = train_size

        self._subvector_dim = dimension // n_subvectors

        # Codebooks: one per subvector, shape (n_clusters, subvector_dim)
        self._codebooks: Optional[List[np.ndarray]] = None

    @property
    def compression_ratio(self) -> float:
        """Compression ratio depends on n_subvectors."""
        original_bytes = self.dimension * 4  # float32
        compressed_bytes = self.n_subvectors  # uint8 per subvector
        return original_bytes / compressed_bytes

    @property
    def code_size(self) -> int:
        """Size of encoded vector in bytes."""
        return self.n_subvectors

    def fit(self, vectors: np.ndarray) -> "ProductQuantizer":
        """
        Train codebooks using k-means on each subvector.

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

        # Use subset for training
        if len(vectors) > self.train_size:
            indices = np.random.choice(
                len(vectors), self.train_size, replace=False
            )
            vectors = vectors[indices]

        # Split into subvectors and train codebook for each
        self._codebooks = []

        for m in range(self.n_subvectors):
            start = m * self._subvector_dim
            end = start + self._subvector_dim
            subvectors = vectors[:, start:end]

            # Train codebook using k-means
            codebook = self._train_codebook(subvectors)
            self._codebooks.append(codebook)

        self._is_fitted = True
        return self

    def _train_codebook(self, subvectors: np.ndarray) -> np.ndarray:
        """
        Train a single codebook using k-means.

        Args:
            subvectors: Subvector data, shape (n, subvector_dim)

        Returns:
            Codebook centroids, shape (n_clusters, subvector_dim)
        """
        n_samples = len(subvectors)

        # Initialize centroids using k-means++
        centroids = self._kmeans_plusplus_init(subvectors)

        for _ in range(self.n_iterations):
            # Assign to nearest centroid
            assignments = self._assign_to_centroids(subvectors, centroids)

            # Update centroids
            new_centroids = np.zeros_like(centroids)
            counts = np.zeros(self.n_clusters)

            for i in range(n_samples):
                cluster = assignments[i]
                new_centroids[cluster] += subvectors[i]
                counts[cluster] += 1

            # Avoid division by zero
            counts = np.maximum(counts, 1)
            new_centroids = new_centroids / counts[:, np.newaxis]

            # Handle empty clusters by reinitializing
            empty_clusters = counts < 1
            if np.any(empty_clusters):
                # Reinitialize from random samples
                n_empty = np.sum(empty_clusters)
                random_indices = np.random.choice(n_samples, n_empty, replace=False)
                new_centroids[empty_clusters] = subvectors[random_indices]

            centroids = new_centroids

        return centroids

    def _kmeans_plusplus_init(self, subvectors: np.ndarray) -> np.ndarray:
        """
        Initialize centroids using k-means++ algorithm.

        Args:
            subvectors: Data points

        Returns:
            Initial centroids
        """
        n_samples = len(subvectors)
        centroids = np.zeros((self.n_clusters, self._subvector_dim), dtype=np.float32)

        # First centroid: random sample
        centroids[0] = subvectors[np.random.randint(n_samples)]

        # Remaining centroids: weighted by squared distance
        for k in range(1, self.n_clusters):
            # Compute squared distances to nearest centroid
            distances = np.min(
                np.sum((subvectors[:, np.newaxis] - centroids[:k]) ** 2, axis=2),
                axis=1
            )

            # Sample proportional to squared distance
            probs = distances / (np.sum(distances) + 1e-8)
            idx = np.random.choice(n_samples, p=probs)
            centroids[k] = subvectors[idx]

        return centroids

    def _assign_to_centroids(
        self,
        subvectors: np.ndarray,
        centroids: np.ndarray
    ) -> np.ndarray:
        """
        Assign subvectors to nearest centroid.

        Args:
            subvectors: Data points, shape (n, subvector_dim)
            centroids: Centroids, shape (n_clusters, subvector_dim)

        Returns:
            Assignments, shape (n,), dtype=uint8
        """
        # Compute squared distances using broadcasting
        # (n, 1, d) - (1, k, d) -> (n, k, d) -> sum -> (n, k)
        sq_distances = np.sum(
            (subvectors[:, np.newaxis, :] - centroids[np.newaxis, :, :]) ** 2,
            axis=2
        )

        return np.argmin(sq_distances, axis=1).astype(np.uint8)

    def encode(self, vectors: np.ndarray) -> np.ndarray:
        """
        Encode vectors to codebook indices.

        Args:
            vectors: Input vectors, shape (n, dimension), dtype=float32

        Returns:
            Quantized codes, shape (n, n_subvectors), dtype=uint8
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        vectors = np.asarray(vectors, dtype=np.float32)

        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        n_vectors = vectors.shape[0]
        codes = np.zeros((n_vectors, self.n_subvectors), dtype=np.uint8)

        for m in range(self.n_subvectors):
            start = m * self._subvector_dim
            end = start + self._subvector_dim
            subvectors = vectors[:, start:end]

            codes[:, m] = self._assign_to_centroids(subvectors, self._codebooks[m])

        return codes

    def decode(self, codes: np.ndarray) -> np.ndarray:
        """
        Reconstruct vectors from codebook indices.

        Args:
            codes: Quantized codes, shape (n, n_subvectors), dtype=uint8

        Returns:
            Reconstructed vectors, shape (n, dimension), dtype=float32
        """
        if not self._is_fitted:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        codes = np.asarray(codes)

        if codes.ndim == 1:
            codes = codes.reshape(1, -1)

        n_vectors = codes.shape[0]
        vectors = np.zeros((n_vectors, self.dimension), dtype=np.float32)

        for m in range(self.n_subvectors):
            start = m * self._subvector_dim
            end = start + self._subvector_dim

            # Lookup centroids
            vectors[:, start:end] = self._codebooks[m][codes[:, m]]

        return vectors

    def compute_distances(
        self,
        query: np.ndarray,
        codes: np.ndarray,
        metric: str = "cosine"
    ) -> np.ndarray:
        """
        Compute distances using Asymmetric Distance Computation (ADC).

        Pre-computes distance tables for query subvectors to codebook entries,
        then uses table lookups for fast distance computation.

        Args:
            query: Query vector (not quantized), shape (dimension,)
            codes: Quantized database vectors, shape (n, n_subvectors)
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

        # Build distance tables for each subvector
        # Table shape: (n_subvectors, n_clusters)
        distance_tables = self._build_distance_tables(query, metric)

        # Lookup and sum distances
        n_vectors = codes.shape[0]
        distances = np.zeros(n_vectors, dtype=np.float32)

        for m in range(self.n_subvectors):
            distances += distance_tables[m, codes[:, m]]

        # Post-process based on metric
        if metric == "cosine":
            # We computed negative dot products, convert to cosine distance
            # Need to normalize by query and codebook norms
            query_norm = np.linalg.norm(query)
            # Approximate: assume unit norm for simplicity
            distances = 1.0 - (-distances / (query_norm + 1e-8))

        return distances

    def _build_distance_tables(
        self,
        query: np.ndarray,
        metric: str
    ) -> np.ndarray:
        """
        Build distance lookup tables for ADC.

        Args:
            query: Query vector, shape (dimension,)
            metric: Distance metric

        Returns:
            Distance tables, shape (n_subvectors, n_clusters)
        """
        tables = np.zeros((self.n_subvectors, self.n_clusters), dtype=np.float32)

        for m in range(self.n_subvectors):
            start = m * self._subvector_dim
            end = start + self._subvector_dim
            query_sub = query[start:end]

            if metric == "euclidean":
                # Squared L2 distance
                diff = self._codebooks[m] - query_sub
                tables[m] = np.sum(diff ** 2, axis=1)

            elif metric == "dot" or metric == "cosine":
                # Negative dot product (negate to use as "distance")
                tables[m] = -np.dot(self._codebooks[m], query_sub)

            else:
                raise ValueError(f"Unknown metric: {metric}")

        return tables

    def save(self, path: Path) -> None:
        """Save quantizer state to disk."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)

        # Save configuration
        config = {
            "dimension": self.dimension,
            "n_subvectors": self.n_subvectors,
            "n_clusters": self.n_clusters,
            "n_iterations": self.n_iterations,
            "train_size": self.train_size,
            "subvector_dim": self._subvector_dim,
            "is_fitted": self._is_fitted,
        }
        with open(path / "config.json", "w") as f:
            json.dump(config, f)

        # Save codebooks
        if self._is_fitted:
            for m, codebook in enumerate(self._codebooks):
                np.save(path / f"codebook_{m}.npy", codebook)

    def load(self, path: Path) -> "ProductQuantizer":
        """Load quantizer state from disk."""
        path = Path(path)

        # Load configuration
        with open(path / "config.json", "r") as f:
            config = json.load(f)

        self.dimension = config["dimension"]
        self.n_subvectors = config["n_subvectors"]
        self.n_clusters = config["n_clusters"]
        self.n_iterations = config["n_iterations"]
        self.train_size = config["train_size"]
        self._subvector_dim = config["subvector_dim"]
        self._is_fitted = config["is_fitted"]

        # Load codebooks
        if self._is_fitted:
            self._codebooks = []
            for m in range(self.n_subvectors):
                codebook = np.load(path / f"codebook_{m}.npy")
                self._codebooks.append(codebook)

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

    def get_codebook_stats(self) -> dict:
        """Get statistics about trained codebooks."""
        if not self._is_fitted:
            return {"fitted": False}

        stats = {
            "fitted": True,
            "n_subvectors": self.n_subvectors,
            "n_clusters": self.n_clusters,
            "subvector_dim": self._subvector_dim,
            "compression_ratio": self.compression_ratio,
            "code_size_bytes": self.code_size,
        }

        # Per-codebook statistics
        codebook_stats = []
        for m, codebook in enumerate(self._codebooks):
            codebook_stats.append({
                "index": m,
                "centroid_mean_norm": float(np.mean(np.linalg.norm(codebook, axis=1))),
                "centroid_std_norm": float(np.std(np.linalg.norm(codebook, axis=1))),
            })

        stats["codebooks"] = codebook_stats

        return stats
