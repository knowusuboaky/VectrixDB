"""
Benchmark Datasets

Standard test datasets and generators for benchmarking.
"""

from typing import Dict, List, Optional, Tuple, Any
import numpy as np


class BenchmarkDatasets:
    """
    Standard datasets for benchmarking.

    Provides random, clustered, and real-world datasets for testing.
    """

    @staticmethod
    def random_vectors(
        n: int,
        dimension: int,
        normalize: bool = True,
        seed: int = 42,
    ) -> np.ndarray:
        """
        Generate random normalized vectors.

        Args:
            n: Number of vectors
            dimension: Vector dimension
            normalize: Whether to L2 normalize
            seed: Random seed

        Returns:
            Vectors array, shape (n, dimension)
        """
        rng = np.random.default_rng(seed)
        vectors = rng.standard_normal((n, dimension)).astype(np.float32)

        if normalize:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / (norms + 1e-8)

        return vectors

    @staticmethod
    def clustered_vectors(
        n: int,
        dimension: int,
        n_clusters: int = 100,
        cluster_std: float = 0.1,
        normalize: bool = True,
        seed: int = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate clustered vectors with ground truth labels.

        Creates vectors grouped around cluster centers, useful for
        testing search quality.

        Args:
            n: Number of vectors
            dimension: Vector dimension
            n_clusters: Number of clusters
            cluster_std: Standard deviation within clusters
            normalize: Whether to L2 normalize
            seed: Random seed

        Returns:
            Tuple of (vectors, labels)
        """
        rng = np.random.default_rng(seed)

        # Generate cluster centers
        centers = rng.standard_normal((n_clusters, dimension)).astype(np.float32)

        # Assign points to clusters
        labels = rng.integers(0, n_clusters, size=n)

        # Generate points around centers
        vectors = centers[labels] + rng.normal(0, cluster_std, (n, dimension)).astype(np.float32)

        if normalize:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / (norms + 1e-8)

        return vectors, labels

    @staticmethod
    def query_with_ground_truth(
        base_vectors: np.ndarray,
        n_queries: int,
        k: int = 10,
        seed: int = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate queries with ground truth nearest neighbors.

        Args:
            base_vectors: Database vectors
            n_queries: Number of queries to generate
            k: Number of ground truth neighbors
            seed: Random seed

        Returns:
            Tuple of (queries, ground_truth_indices)
        """
        rng = np.random.default_rng(seed)

        # Generate random queries (slight perturbations of base vectors)
        query_indices = rng.choice(len(base_vectors), size=n_queries, replace=False)
        queries = base_vectors[query_indices].copy()

        # Add small noise to queries
        queries += rng.normal(0, 0.01, queries.shape).astype(np.float32)

        # Normalize
        norms = np.linalg.norm(queries, axis=1, keepdims=True)
        queries = queries / (norms + 1e-8)

        # Compute ground truth using brute force
        # For large datasets, this is slow but accurate
        ground_truth = []

        for query in queries:
            # Cosine similarity (dot product for normalized vectors)
            similarities = np.dot(base_vectors, query)
            top_k = np.argsort(-similarities)[:k]
            ground_truth.append(top_k)

        return queries, np.array(ground_truth)

    @staticmethod
    def generate_metadata(
        n: int,
        schema: Dict[str, str],
        seed: int = 42,
    ) -> List[Dict[str, Any]]:
        """
        Generate random metadata matching a schema.

        Args:
            n: Number of records
            schema: Field name -> type mapping
                Types: "int", "float", "string", "bool", "tags"
            seed: Random seed

        Returns:
            List of metadata dicts
        """
        rng = np.random.default_rng(seed)

        string_values = [
            "category_a", "category_b", "category_c", "category_d", "category_e"
        ]
        tag_values = ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8"]

        metadata = []

        for i in range(n):
            record = {}

            for field, dtype in schema.items():
                if dtype == "int":
                    record[field] = int(rng.integers(0, 1000))
                elif dtype == "float":
                    record[field] = float(rng.uniform(0, 1000))
                elif dtype == "string":
                    record[field] = rng.choice(string_values)
                elif dtype == "bool":
                    record[field] = bool(rng.choice([True, False]))
                elif dtype == "tags":
                    n_tags = rng.integers(1, 5)
                    record[field] = list(rng.choice(tag_values, size=n_tags, replace=False))

            metadata.append(record)

        return metadata

    @staticmethod
    def glove_sample(
        n: int = 10000,
        dimension: int = 100,
    ) -> np.ndarray:
        """
        Generate GloVe-like word embeddings (synthetic).

        Creates embeddings with similar statistical properties to real GloVe.

        Args:
            n: Number of vectors
            dimension: Vector dimension (50, 100, 200, 300)

        Returns:
            Vectors array
        """
        rng = np.random.default_rng(42)

        # GloVe-like distribution: mostly small values with some outliers
        vectors = rng.standard_normal((n, dimension)).astype(np.float32)

        # Scale by frequency-like distribution
        frequencies = rng.power(0.7, n)  # Zipf-like
        vectors = vectors * frequencies[:, np.newaxis]

        # Clip extreme values
        vectors = np.clip(vectors, -5, 5)

        return vectors

    @staticmethod
    def sentence_embeddings_sample(
        n: int = 10000,
        dimension: int = 384,
        seed: int = 42,
    ) -> np.ndarray:
        """
        Generate sentence embedding-like vectors (synthetic).

        Creates embeddings with properties similar to sentence-transformers output.

        Args:
            n: Number of vectors
            dimension: Vector dimension (typically 384 or 768)
            seed: Random seed

        Returns:
            Normalized vectors
        """
        rng = np.random.default_rng(seed)

        # Sentence embeddings are typically normalized
        vectors = rng.standard_normal((n, dimension)).astype(np.float32)

        # Add some structure (simulating semantic clusters)
        n_topics = 50
        topics = rng.standard_normal((n_topics, dimension)).astype(np.float32)
        topic_assignments = rng.integers(0, n_topics, size=n)
        topic_weights = rng.uniform(0.3, 0.7, size=n)

        vectors = (1 - topic_weights[:, np.newaxis]) * vectors + \
                  topic_weights[:, np.newaxis] * topics[topic_assignments]

        # Normalize
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / (norms + 1e-8)

        return vectors

    @staticmethod
    def sparse_vectors(
        n: int,
        dimension: int,
        sparsity: float = 0.95,
        seed: int = 42,
    ) -> List[Dict[int, float]]:
        """
        Generate sparse vectors.

        Args:
            n: Number of vectors
            dimension: Maximum dimension
            sparsity: Fraction of zeros (0.95 = 5% non-zero)
            seed: Random seed

        Returns:
            List of sparse vectors as {index: value} dicts
        """
        rng = np.random.default_rng(seed)

        n_nonzero = int(dimension * (1 - sparsity))
        vectors = []

        for _ in range(n):
            indices = rng.choice(dimension, size=n_nonzero, replace=False)
            values = rng.uniform(0.1, 1.0, size=n_nonzero)

            sparse = {int(idx): float(val) for idx, val in zip(indices, values)}
            vectors.append(sparse)

        return vectors


class DatasetScaler:
    """
    Scale datasets for different benchmark sizes.
    """

    SIZES = {
        "tiny": 1_000,
        "small": 10_000,
        "medium": 100_000,
        "large": 1_000_000,
        "xlarge": 10_000_000,
    }

    @classmethod
    def get_size(cls, size_name: str) -> int:
        """Get vector count for a size name."""
        return cls.SIZES.get(size_name, 10_000)

    @classmethod
    def create_dataset(
        cls,
        size: str,
        dimension: int = 384,
        dataset_type: str = "random",
    ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """
        Create a dataset of specified size.

        Args:
            size: Size name (tiny, small, medium, large, xlarge)
            dimension: Vector dimension
            dataset_type: Type (random, clustered, sentence)

        Returns:
            Tuple of (vectors, labels or None)
        """
        n = cls.get_size(size)

        if dataset_type == "random":
            return BenchmarkDatasets.random_vectors(n, dimension), None
        elif dataset_type == "clustered":
            return BenchmarkDatasets.clustered_vectors(n, dimension)
        elif dataset_type == "sentence":
            return BenchmarkDatasets.sentence_embeddings_sample(n, dimension), None
        else:
            return BenchmarkDatasets.random_vectors(n, dimension), None
