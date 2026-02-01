"""
Dense Vector Search

Advanced dense search capabilities including multi-query,
negative queries, and prefetch+rescore patterns.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import numpy as np


@dataclass
class SearchResult:
    """Single search result."""
    id: str
    score: float
    vector: Optional[np.ndarray] = None
    payload: Optional[Dict[str, Any]] = None


@dataclass
class DenseSearchConfig:
    """Configuration for dense search."""
    metric: str = "cosine"  # cosine, euclidean, dot
    ef_search: int = 100
    use_quantization: bool = False
    rescore_multiplier: int = 4  # Prefetch this many candidates for rescoring


class DenseSearch:
    """
    Dense vector search with advanced features.

    Supports multi-query, negative queries, and prefetch+rescore patterns.

    Example:
        >>> search = DenseSearch(index, vectors)
        >>> results = search.search(query_vector, k=10)
        >>> results = search.search_with_negatives(
        ...     positive=query_vector,
        ...     negative=[neg_vector],
        ...     k=10
        ... )
    """

    def __init__(
        self,
        index: Any,  # HNSWIndex or similar
        vectors: np.ndarray,
        ids: Optional[List[str]] = None,
        config: Optional[DenseSearchConfig] = None,
    ):
        """
        Initialize dense search.

        Args:
            index: Vector index (HNSW or similar)
            vectors: Dense vectors array
            ids: Vector IDs (optional)
            config: Search configuration
        """
        self.index = index
        self.vectors = vectors
        self.ids = ids or [str(i) for i in range(len(vectors))]
        self.config = config or DenseSearchConfig()

        # Build ID to index mapping
        self._id_to_idx = {id_: idx for idx, id_ in enumerate(self.ids)}

    def search(
        self,
        query: np.ndarray,
        k: int = 10,
        filter_ids: Optional[List[str]] = None,
        ef: Optional[int] = None,
    ) -> List[SearchResult]:
        """
        Basic dense search.

        Args:
            query: Query vector
            k: Number of results
            filter_ids: Only search within these IDs
            ef: Override ef_search parameter

        Returns:
            List of SearchResult
        """
        query = np.asarray(query, dtype=np.float32)

        if filter_ids is not None:
            # Filtered search using brute force on subset
            return self._filtered_search(query, k, filter_ids)

        # Use index for search
        if hasattr(self.index, 'search'):
            indices, distances = self.index.search(
                query.reshape(1, -1),
                k=k,
                ef=ef or self.config.ef_search
            )
            indices = indices[0]
            distances = distances[0]
        else:
            # Fallback to brute force
            indices, distances = self._brute_force_search(query, k)

        results = []
        for idx, dist in zip(indices, distances):
            if idx < 0:  # Invalid index
                continue
            score = self._distance_to_score(dist)
            results.append(SearchResult(
                id=self.ids[idx],
                score=score,
                vector=self.vectors[idx],
            ))

        return results

    def search_with_negatives(
        self,
        positive: np.ndarray,
        negative: List[np.ndarray],
        k: int = 10,
        negative_weight: float = 0.5,
    ) -> List[SearchResult]:
        """
        Search with negative query vectors.

        Adjusts the query to move away from negative examples.

        Args:
            positive: Positive query vector
            negative: List of negative vectors to avoid
            k: Number of results
            negative_weight: Weight for negative vectors (0-1)

        Returns:
            List of SearchResult
        """
        positive = np.asarray(positive, dtype=np.float32)

        # Compute adjusted query
        adjusted_query = positive.copy()

        if negative:
            # Average negative vectors
            neg_array = np.array(negative, dtype=np.float32)
            neg_centroid = np.mean(neg_array, axis=0)

            # Move query away from negative centroid
            adjusted_query = positive - (negative_weight * neg_centroid)

            # Normalize
            norm = np.linalg.norm(adjusted_query)
            if norm > 0:
                adjusted_query = adjusted_query / norm

        return self.search(adjusted_query, k=k)

    def search_batch(
        self,
        queries: np.ndarray,
        k: int = 10,
    ) -> List[List[SearchResult]]:
        """
        Batch search for multiple queries.

        Args:
            queries: Array of query vectors
            k: Number of results per query

        Returns:
            List of result lists
        """
        queries = np.asarray(queries, dtype=np.float32)
        results = []

        # If index supports batch search
        if hasattr(self.index, 'search') and len(queries.shape) == 2:
            all_indices, all_distances = self.index.search(queries, k=k)

            for indices, distances in zip(all_indices, all_distances):
                query_results = []
                for idx, dist in zip(indices, distances):
                    if idx < 0:
                        continue
                    score = self._distance_to_score(dist)
                    query_results.append(SearchResult(
                        id=self.ids[idx],
                        score=score,
                    ))
                results.append(query_results)
        else:
            # Sequential fallback
            for query in queries:
                results.append(self.search(query, k=k))

        return results

    def _filtered_search(
        self,
        query: np.ndarray,
        k: int,
        filter_ids: List[str],
    ) -> List[SearchResult]:
        """Search within a filtered subset of vectors."""
        # Get indices for filtered IDs
        filtered_indices = [
            self._id_to_idx[id_]
            for id_ in filter_ids
            if id_ in self._id_to_idx
        ]

        if not filtered_indices:
            return []

        # Brute force on subset
        filtered_vectors = self.vectors[filtered_indices]
        distances = self._compute_distances(query, filtered_vectors)

        # Get top k
        top_k_local = np.argsort(distances)[:k]

        results = []
        for local_idx in top_k_local:
            global_idx = filtered_indices[local_idx]
            score = self._distance_to_score(distances[local_idx])
            results.append(SearchResult(
                id=self.ids[global_idx],
                score=score,
                vector=self.vectors[global_idx],
            ))

        return results

    def _brute_force_search(
        self,
        query: np.ndarray,
        k: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Brute force search over all vectors."""
        distances = self._compute_distances(query, self.vectors)
        top_k = np.argsort(distances)[:k]
        return top_k, distances[top_k]

    def _compute_distances(
        self,
        query: np.ndarray,
        vectors: np.ndarray,
    ) -> np.ndarray:
        """Compute distances based on metric."""
        if self.config.metric == "cosine":
            # Cosine distance = 1 - cosine similarity
            query_norm = query / (np.linalg.norm(query) + 1e-8)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors_norm = vectors / (norms + 1e-8)
            similarities = np.dot(vectors_norm, query_norm)
            return 1 - similarities

        elif self.config.metric == "euclidean":
            return np.linalg.norm(vectors - query, axis=1)

        elif self.config.metric == "dot":
            # Negative dot product (higher = more similar, so negate for distance)
            return -np.dot(vectors, query)

        else:
            raise ValueError(f"Unknown metric: {self.config.metric}")

    def _distance_to_score(self, distance: float) -> float:
        """Convert distance to similarity score."""
        if self.config.metric == "cosine":
            return 1 - distance
        elif self.config.metric == "euclidean":
            return 1 / (1 + distance)
        elif self.config.metric == "dot":
            return -distance  # Was negated
        return 1 / (1 + distance)


class MultiQuerySearch:
    """
    Multi-query search with result aggregation.

    Supports multiple query strategies: max, mean, weighted.

    Example:
        >>> mq = MultiQuerySearch(dense_search)
        >>> results = mq.search([query1, query2, query3], k=10, strategy="max")
    """

    def __init__(self, dense_search: DenseSearch):
        """
        Initialize multi-query search.

        Args:
            dense_search: Underlying dense search instance
        """
        self.dense_search = dense_search

    def search(
        self,
        queries: List[np.ndarray],
        k: int = 10,
        strategy: str = "max",
        weights: Optional[List[float]] = None,
    ) -> List[SearchResult]:
        """
        Search with multiple queries and aggregate results.

        Args:
            queries: List of query vectors
            k: Number of final results
            strategy: Aggregation strategy (max, mean, weighted)
            weights: Query weights for weighted strategy

        Returns:
            Aggregated search results
        """
        if not queries:
            return []

        # Get more candidates per query
        candidates_per_query = k * 2

        # Collect all results
        all_results: Dict[str, List[float]] = {}

        for i, query in enumerate(queries):
            results = self.dense_search.search(
                np.asarray(query, dtype=np.float32),
                k=candidates_per_query
            )

            weight = weights[i] if weights else 1.0

            for result in results:
                if result.id not in all_results:
                    all_results[result.id] = []
                all_results[result.id].append(result.score * weight)

        # Aggregate scores
        aggregated = []

        for id_, scores in all_results.items():
            if strategy == "max":
                final_score = max(scores)
            elif strategy == "mean":
                final_score = sum(scores) / len(scores)
            elif strategy == "weighted":
                # Already weighted above
                final_score = sum(scores) / (sum(weights) if weights else len(scores))
            else:
                final_score = max(scores)

            aggregated.append((id_, final_score))

        # Sort by score
        aggregated.sort(key=lambda x: x[1], reverse=True)

        # Return top k
        results = []
        for id_, score in aggregated[:k]:
            idx = self.dense_search._id_to_idx.get(id_)
            vector = self.dense_search.vectors[idx] if idx is not None else None
            results.append(SearchResult(id=id_, score=score, vector=vector))

        return results


class PrefetchRescore:
    """
    Prefetch + rescore pattern for improved accuracy.

    First retrieves more candidates with approximate search,
    then rescores with exact distance computation.

    Example:
        >>> pr = PrefetchRescore(index, vectors)
        >>> results = pr.search(query, k=10, prefetch_k=100)
    """

    def __init__(
        self,
        index: Any,
        vectors: np.ndarray,
        ids: Optional[List[str]] = None,
        metric: str = "cosine",
    ):
        """
        Initialize prefetch+rescore.

        Args:
            index: Approximate search index
            vectors: Full precision vectors for rescoring
            ids: Vector IDs
            metric: Distance metric
        """
        self.index = index
        self.vectors = vectors
        self.ids = ids or [str(i) for i in range(len(vectors))]
        self.metric = metric

    def search(
        self,
        query: np.ndarray,
        k: int = 10,
        prefetch_k: Optional[int] = None,
        rescore_fn: Optional[Callable[[np.ndarray, np.ndarray], float]] = None,
    ) -> List[SearchResult]:
        """
        Search with prefetch and rescore.

        Args:
            query: Query vector
            k: Number of final results
            prefetch_k: Number of candidates to prefetch (default: 4x k)
            rescore_fn: Custom rescoring function

        Returns:
            Rescored search results
        """
        query = np.asarray(query, dtype=np.float32)
        prefetch_k = prefetch_k or (k * 4)

        # Phase 1: Prefetch candidates with approximate search
        if hasattr(self.index, 'search'):
            indices, _ = self.index.search(query.reshape(1, -1), k=prefetch_k)
            candidate_indices = indices[0]
        else:
            # Brute force fallback
            distances = self._compute_distances(query, self.vectors)
            candidate_indices = np.argsort(distances)[:prefetch_k]

        # Filter invalid indices
        candidate_indices = [idx for idx in candidate_indices if idx >= 0]

        if not candidate_indices:
            return []

        # Phase 2: Rescore candidates with exact computation
        rescored = []

        for idx in candidate_indices:
            if rescore_fn:
                score = rescore_fn(query, self.vectors[idx])
            else:
                score = self._exact_score(query, self.vectors[idx])

            rescored.append((idx, score))

        # Sort by rescored score
        rescored.sort(key=lambda x: x[1], reverse=True)

        # Return top k
        results = []
        for idx, score in rescored[:k]:
            results.append(SearchResult(
                id=self.ids[idx],
                score=score,
                vector=self.vectors[idx],
            ))

        return results

    def _compute_distances(
        self,
        query: np.ndarray,
        vectors: np.ndarray,
    ) -> np.ndarray:
        """Compute approximate distances."""
        if self.metric == "cosine":
            similarities = np.dot(vectors, query) / (
                np.linalg.norm(vectors, axis=1) * np.linalg.norm(query) + 1e-8
            )
            return 1 - similarities
        elif self.metric == "euclidean":
            return np.linalg.norm(vectors - query, axis=1)
        else:
            return -np.dot(vectors, query)

    def _exact_score(
        self,
        query: np.ndarray,
        vector: np.ndarray,
    ) -> float:
        """Compute exact similarity score."""
        if self.metric == "cosine":
            return float(np.dot(query, vector) / (
                np.linalg.norm(query) * np.linalg.norm(vector) + 1e-8
            ))
        elif self.metric == "euclidean":
            return 1.0 / (1.0 + float(np.linalg.norm(query - vector)))
        else:  # dot
            return float(np.dot(query, vector))
