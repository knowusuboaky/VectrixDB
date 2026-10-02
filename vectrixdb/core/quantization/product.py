"""
Product Quantizer (PQ)

Splits vectors into subvectors and quantizes each to a codebook entry.
Provides configurable compression with good accuracy retention.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple
import numpy as np

from .base import BaseQuantizer


__all__ = [
    "ProductQuantizer",
]


# ============================================================================
# PRODUCT QUANTIZATION
# ============================================================================
#
# INPUT   float32 vectors
# OUTPUT  subvectors, each replaced by the nearest codebook entry:
#         configurable compression that keeps accuracy
#
# Trained on a sample; asymmetric distance at query time.


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
            raise ValueError(f"n_clusters must be <= 256 for uint8 codes, got {n_clusters}")

        self.n_subvectors = n_subvectors
        self.n_clusters = n_clusters
        self.n_iterations = n_iterations
        self.train_size = train_size

        self._subvector_dim = dimension // n_subvectors

        # Codebooks: one per subvector, shape (n_clusters, subvector_dim)
        self._codebooks: Optional[List[np.ndarray]] = None
        self._centroid_sq_norms: np.ndarray = np.zeros(
            (self.n_subvectors, self.n_clusters), dtype=np.float32
        )

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
            raise ValueError(f"Expected dimension {self.dimension}, got {vectors.shape[1]}")

        # Use subset for training
        if len(vectors) > self.train_size:
            indices = np.random.choice(len(vectors), self.train_size, replace=False)
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

        self._cache_centroid_norms()
        self._is_fitted = True
        return self

    def _cache_centroid_norms(self) -> None:
        """Squared norm of every centroid, for the cosine denominator.

        A reconstructed vector is one centroid per subvector concatenated, so
        its squared norm is the sum of these. Computed once here rather than
        per query.
        """
        if not self._codebooks:
            self._centroid_sq_norms = np.zeros((self.n_subvectors, self.n_clusters), np.float32)
            return
        self._centroid_sq_norms = np.stack(
            [np.sum(book.astype(np.float32) ** 2, axis=1) for book in self._codebooks]
        )

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

            # Update centroids: one scatter-add instead of a Python loop.
            new_centroids = np.zeros_like(centroids)
            np.add.at(new_centroids, assignments, subvectors)
            counts = np.bincount(assignments, minlength=self.n_clusters).astype(np.float32)

            # Avoid division by zero
            counts = np.maximum(counts, 1)
            new_centroids = new_centroids / counts[:, np.newaxis]

            # Handle empty clusters by reinitializing
            empty_clusters = counts < 1
            if np.any(empty_clusters):
                # Reinitialize from random samples
                n_empty = int(np.sum(empty_clusters))
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

        # Remaining centroids, weighted by squared distance to the nearest one
        # chosen so far. That minimum is kept running and refreshed against the
        # newest centroid only; recomputing it against all k at every step was
        # O(n k^2 d) and dominated fit() outright.
        min_d2 = np.sum((subvectors - centroids[0]) ** 2, axis=1)
        for k in range(1, self.n_clusters):
            total = float(np.sum(min_d2))
            if total <= 0.0:
                # Every distinct point is already a centroid, so there is no
                # distance left to weight by. Dividing by the sum here gave a
                # probability vector of zeros and numpy refused it with
                # "probabilities do not sum to 1", which is a long way from
                # the real cause. It is not a small-corpus problem either:
                # fifty thousand vectors drawn from a forty-entry palette hit
                # it too. The remaining centroids are filled uniformly; they
                # duplicate points no sample will prefer, which costs a
                # little codebook space and nothing else.
                idx = int(np.random.randint(n_samples))
            else:
                idx = int(np.random.choice(n_samples, p=min_d2 / total))
            centroids[k] = subvectors[idx]
            min_d2 = np.minimum(min_d2, np.sum((subvectors - centroids[k]) ** 2, axis=1))

        return centroids

    def _assign_to_centroids(self, subvectors: np.ndarray, centroids: np.ndarray) -> np.ndarray:
        """
        Assign subvectors to nearest centroid.

        Args:
            subvectors: Data points, shape (n, subvector_dim)
            centroids: Centroids, shape (n_clusters, subvector_dim)

        Returns:
            Assignments, shape (n,), dtype=uint8
        """
        # ||a - b||^2 = ||a||^2 + ||b||^2 - 2 a.b, as one (n, k) matrix product
        # rather than an (n, k, d) difference tensor, which was 245 MB at n=5000.
        a2 = np.sum(subvectors * subvectors, axis=1)[:, np.newaxis]
        b2 = np.sum(centroids * centroids, axis=1)[np.newaxis, :]
        sq_distances = a2 + b2 - 2.0 * (subvectors @ centroids.T)

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

        codebooks = self._codebooks
        if codebooks is None:
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

            codes[:, m] = self._assign_to_centroids(subvectors, codebooks[m])

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

        codebooks = self._codebooks
        if codebooks is None:
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
            vectors[:, start:end] = codebooks[m][codes[:, m]]

        return vectors

    def compute_distances(
        self, query: np.ndarray, codes: np.ndarray, metric: str = "cosine"
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
        if metric == "euclidean":
            # The tables hold squared L2, which is what the lookup sums. Take
            # the root so this agrees with ScalarQuantizer, which returns the
            # distance itself. Ranking is unchanged either way; a caller
            # comparing the two quantisers against one threshold was not.
            distances = np.sqrt(np.maximum(distances, 0.0)).astype(np.float32)

        if metric == "cosine":
            # The table holds negative dot products, so -distances is the dot
            # product between the query and each reconstructed vector. Cosine
            # divides that by both norms. Dividing by the query norm alone,
            # which is what this did while calling it an approximation, ranks
            # by projection rather than angle: two vectors pointing the same
            # way got different distances if one was longer, and the result
            # could fall outside the valid range, negatives included.
            #
            # A reconstructed vector is the concatenation of one centroid per
            # subvector, so its squared norm is the sum of those centroids'
            # squared norms, looked up exactly like the distances above.
            query_norm = float(np.linalg.norm(query))
            recon_sq = np.zeros(n_vectors, dtype=np.float32)
            for m in range(self.n_subvectors):
                recon_sq += self._centroid_sq_norms[m, codes[:, m]]
            recon_norm = np.sqrt(recon_sq)
            denominator = query_norm * recon_norm
            cosine = np.divide(
                -distances,
                denominator,
                out=np.zeros_like(distances),
                where=denominator > 0,
            )
            distances = (1.0 - np.clip(cosine, -1.0, 1.0)).astype(np.float32)

        return distances

    def _build_distance_tables(self, query: np.ndarray, metric: str) -> np.ndarray:
        """
        Build distance lookup tables for ADC.

        Args:
            query: Query vector, shape (dimension,)
            metric: Distance metric

        Returns:
            Distance tables, shape (n_subvectors, n_clusters)
        """
        codebooks = self._codebooks
        if codebooks is None:
            raise RuntimeError("Quantizer not fitted. Call fit() first.")

        tables = np.zeros((self.n_subvectors, self.n_clusters), dtype=np.float32)

        for m in range(self.n_subvectors):
            start = m * self._subvector_dim
            end = start + self._subvector_dim
            query_sub = query[start:end]

            if metric == "euclidean":
                # Squared L2 distance
                diff = codebooks[m] - query_sub
                tables[m] = np.sum(diff**2, axis=1)

            elif metric == "dot" or metric == "cosine":
                # Negative dot product (negate to use as "distance")
                tables[m] = -np.dot(codebooks[m], query_sub)

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
            codebooks = self._codebooks
            if codebooks is None:
                raise RuntimeError("Quantizer state is corrupted: codebooks missing.")
            for m, codebook in enumerate(codebooks):
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

        self._cache_centroid_norms()
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

    def get_codebook_stats(self) -> Dict[str, Any]:
        """Get statistics about trained codebooks."""
        if not self._is_fitted:
            return {"fitted": False}

        codebooks = self._codebooks
        if codebooks is None:
            raise RuntimeError("Quantizer state is corrupted: codebooks missing.")

        stats: Dict[str, Any] = {
            "fitted": True,
            "n_subvectors": self.n_subvectors,
            "n_clusters": self.n_clusters,
            "subvector_dim": self._subvector_dim,
            "compression_ratio": self.compression_ratio,
            "code_size_bytes": self.code_size,
        }

        # Per-codebook statistics
        codebook_stats = []
        for m, codebook in enumerate(codebooks):
            codebook_stats.append(
                {
                    "index": m,
                    "centroid_mean_norm": float(np.mean(np.linalg.norm(codebook, axis=1))),
                    "centroid_std_norm": float(np.std(np.linalg.norm(codebook, axis=1))),
                }
            )

        stats["codebooks"] = codebook_stats

        return stats
