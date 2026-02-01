"""
Search Fusion Strategies

Combines results from multiple search methods using various fusion algorithms.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np


@dataclass
class FusedResult:
    """Result from fused search."""
    id: str
    score: float
    rank: int
    source_scores: Dict[str, float] = field(default_factory=dict)
    source_ranks: Dict[str, int] = field(default_factory=dict)
    payload: Optional[Dict[str, Any]] = None


class FusionStrategy(ABC):
    """
    Abstract base class for fusion strategies.

    Combines ranked results from multiple sources.
    """

    @abstractmethod
    def fuse(
        self,
        result_lists: Dict[str, List[Tuple[str, float]]],
        k: int = 10,
    ) -> List[FusedResult]:
        """
        Fuse results from multiple sources.

        Args:
            result_lists: Dict mapping source name to list of (id, score) tuples
            k: Number of results to return

        Returns:
            Fused results
        """
        pass


class RRFFusion(FusionStrategy):
    """
    Reciprocal Rank Fusion (RRF).

    Combines rankings using: score = sum(1 / (k + rank))
    Works well when sources have different score scales.

    Example:
        >>> rrf = RRFFusion(k=60)
        >>> results = rrf.fuse({
        ...     "dense": [("doc1", 0.9), ("doc2", 0.8)],
        ...     "sparse": [("doc2", 5.5), ("doc1", 4.2)],
        ... })
    """

    def __init__(self, k: int = 60):
        """
        Initialize RRF.

        Args:
            k: RRF constant (typically 60)
        """
        self.k = k

    def fuse(
        self,
        result_lists: Dict[str, List[Tuple[str, float]]],
        k: int = 10,
    ) -> List[FusedResult]:
        """Fuse using Reciprocal Rank Fusion."""
        scores: Dict[str, float] = {}
        source_scores: Dict[str, Dict[str, float]] = {}
        source_ranks: Dict[str, Dict[str, int]] = {}

        for source_name, results in result_lists.items():
            source_scores[source_name] = {}
            source_ranks[source_name] = {}

            for rank, (doc_id, score) in enumerate(results, 1):
                # RRF score contribution
                rrf_score = 1.0 / (self.k + rank)

                if doc_id not in scores:
                    scores[doc_id] = 0.0

                scores[doc_id] += rrf_score
                source_scores[source_name][doc_id] = score
                source_ranks[source_name][doc_id] = rank

        # Sort by fused score
        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for rank, (doc_id, score) in enumerate(sorted_docs[:k], 1):
            results.append(FusedResult(
                id=doc_id,
                score=score,
                rank=rank,
                source_scores={
                    src: source_scores[src].get(doc_id, 0.0)
                    for src in result_lists
                },
                source_ranks={
                    src: source_ranks[src].get(doc_id, -1)
                    for src in result_lists
                },
            ))

        return results


class LinearFusion(FusionStrategy):
    """
    Linear combination fusion.

    Combines scores using: final_score = sum(weight[i] * normalized_score[i])
    Requires score normalization.

    Example:
        >>> fusion = LinearFusion(weights={"dense": 0.7, "sparse": 0.3})
        >>> results = fusion.fuse(result_lists)
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        normalize: str = "minmax",
    ):
        """
        Initialize linear fusion.

        Args:
            weights: Source weights (defaults to equal weights)
            normalize: Normalization method (minmax, zscore, rank)
        """
        self.weights = weights or {}
        self.normalize = normalize

    def fuse(
        self,
        result_lists: Dict[str, List[Tuple[str, float]]],
        k: int = 10,
    ) -> List[FusedResult]:
        """Fuse using linear combination."""
        # Normalize scores
        normalized = {}

        for source_name, results in result_lists.items():
            if not results:
                continue

            scores = [score for _, score in results]

            if self.normalize == "minmax":
                min_score = min(scores)
                max_score = max(scores)
                range_score = max_score - min_score or 1.0
                normalized[source_name] = [
                    (doc_id, (score - min_score) / range_score)
                    for doc_id, score in results
                ]

            elif self.normalize == "zscore":
                mean_score = np.mean(scores)
                std_score = np.std(scores) or 1.0
                normalized[source_name] = [
                    (doc_id, (score - mean_score) / std_score)
                    for doc_id, score in results
                ]

            elif self.normalize == "rank":
                # Rank-based normalization (1.0 for rank 1, decreasing)
                n = len(results)
                normalized[source_name] = [
                    (doc_id, 1.0 - (rank / n))
                    for rank, (doc_id, _) in enumerate(results)
                ]

            else:
                normalized[source_name] = results

        # Compute weights
        weights = {}
        total_weight = 0.0

        for source_name in normalized:
            w = self.weights.get(source_name, 1.0)
            weights[source_name] = w
            total_weight += w

        # Normalize weights
        if total_weight > 0:
            weights = {k: v / total_weight for k, v in weights.items()}

        # Combine scores
        final_scores: Dict[str, float] = {}
        source_scores: Dict[str, Dict[str, float]] = {src: {} for src in normalized}
        source_ranks: Dict[str, Dict[str, int]] = {src: {} for src in normalized}

        for source_name, results in normalized.items():
            weight = weights.get(source_name, 1.0)

            for rank, (doc_id, score) in enumerate(results, 1):
                if doc_id not in final_scores:
                    final_scores[doc_id] = 0.0

                final_scores[doc_id] += weight * score
                source_scores[source_name][doc_id] = score
                source_ranks[source_name][doc_id] = rank

        # Sort by final score
        sorted_docs = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for rank, (doc_id, score) in enumerate(sorted_docs[:k], 1):
            results.append(FusedResult(
                id=doc_id,
                score=score,
                rank=rank,
                source_scores={
                    src: source_scores[src].get(doc_id, 0.0)
                    for src in normalized
                },
                source_ranks={
                    src: source_ranks[src].get(doc_id, -1)
                    for src in normalized
                },
            ))

        return results


class CondorcetFusion(FusionStrategy):
    """
    Condorcet voting fusion.

    Uses pairwise comparisons: A beats B if more sources rank A higher than B.
    Good for combining diverse rankings.

    Example:
        >>> fusion = CondorcetFusion()
        >>> results = fusion.fuse(result_lists)
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        """
        Initialize Condorcet fusion.

        Args:
            weights: Optional source weights for voting
        """
        self.weights = weights or {}

    def fuse(
        self,
        result_lists: Dict[str, List[Tuple[str, float]]],
        k: int = 10,
    ) -> List[FusedResult]:
        """Fuse using Condorcet voting."""
        # Get all unique document IDs
        all_docs = set()
        for results in result_lists.values():
            all_docs.update(doc_id for doc_id, _ in results)

        doc_list = list(all_docs)
        n = len(doc_list)

        if n == 0:
            return []

        # Build rank maps
        rank_maps: Dict[str, Dict[str, int]] = {}

        for source_name, results in result_lists.items():
            rank_maps[source_name] = {
                doc_id: rank
                for rank, (doc_id, _) in enumerate(results, 1)
            }

        # Compute pairwise wins
        wins = np.zeros((n, n), dtype=np.float32)

        for i, doc_a in enumerate(doc_list):
            for j, doc_b in enumerate(doc_list):
                if i == j:
                    continue

                # Count wins for doc_a over doc_b
                for source_name, rank_map in rank_maps.items():
                    weight = self.weights.get(source_name, 1.0)

                    rank_a = rank_map.get(doc_a, float('inf'))
                    rank_b = rank_map.get(doc_b, float('inf'))

                    if rank_a < rank_b:  # Lower rank = better
                        wins[i, j] += weight

        # Compute Condorcet scores (wins minus losses)
        win_counts = wins.sum(axis=1)
        loss_counts = wins.sum(axis=0)
        condorcet_scores = win_counts - loss_counts

        # Sort by Condorcet score
        sorted_indices = np.argsort(-condorcet_scores)

        # Build source score/rank info
        source_scores: Dict[str, Dict[str, float]] = {src: {} for src in result_lists}
        source_ranks: Dict[str, Dict[str, int]] = {src: {} for src in result_lists}

        for source_name, results in result_lists.items():
            for rank, (doc_id, score) in enumerate(results, 1):
                source_scores[source_name][doc_id] = score
                source_ranks[source_name][doc_id] = rank

        results = []
        for rank, idx in enumerate(sorted_indices[:k], 1):
            doc_id = doc_list[idx]
            results.append(FusedResult(
                id=doc_id,
                score=float(condorcet_scores[idx]),
                rank=rank,
                source_scores={
                    src: source_scores[src].get(doc_id, 0.0)
                    for src in result_lists
                },
                source_ranks={
                    src: source_ranks[src].get(doc_id, -1)
                    for src in result_lists
                },
            ))

        return results


class HybridSearcher:
    """
    High-level hybrid search combining multiple search methods.

    Example:
        >>> searcher = HybridSearcher()
        >>> searcher.add_source("dense", dense_search_fn)
        >>> searcher.add_source("sparse", sparse_search_fn)
        >>> results = searcher.search(query, k=10)
    """

    def __init__(
        self,
        fusion_strategy: Optional[FusionStrategy] = None,
        default_k: int = 100,
    ):
        """
        Initialize hybrid searcher.

        Args:
            fusion_strategy: Fusion strategy (defaults to RRF)
            default_k: Default results per source
        """
        self.fusion = fusion_strategy or RRFFusion()
        self.default_k = default_k

        # Search sources: name -> (search_fn, weight)
        self._sources: Dict[str, Tuple[Any, float]] = {}

    def add_source(
        self,
        name: str,
        search_fn: Any,
        weight: float = 1.0,
    ) -> None:
        """
        Add a search source.

        Args:
            name: Source name
            search_fn: Search function that takes (query, k) and returns [(id, score)]
            weight: Source weight for fusion
        """
        self._sources[name] = (search_fn, weight)

    def remove_source(self, name: str) -> bool:
        """Remove a search source."""
        if name in self._sources:
            del self._sources[name]
            return True
        return False

    def search(
        self,
        query: Any,
        k: int = 10,
        source_k: Optional[int] = None,
        sources: Optional[List[str]] = None,
    ) -> List[FusedResult]:
        """
        Perform hybrid search.

        Args:
            query: Search query (passed to all sources)
            k: Number of final results
            source_k: Results per source (default: 10x k)
            sources: Which sources to use (default: all)

        Returns:
            Fused search results
        """
        source_k = source_k or (k * 10)

        # Select sources
        active_sources = (
            {name: self._sources[name] for name in sources if name in self._sources}
            if sources else self._sources
        )

        if not active_sources:
            return []

        # Get results from each source
        result_lists: Dict[str, List[Tuple[str, float]]] = {}

        for name, (search_fn, _) in active_sources.items():
            try:
                results = search_fn(query, source_k)

                # Normalize format to [(id, score)]
                normalized_results = []
                for r in results:
                    if isinstance(r, tuple) and len(r) >= 2:
                        normalized_results.append((r[0], float(r[1])))
                    elif hasattr(r, 'id') and hasattr(r, 'score'):
                        normalized_results.append((r.id, float(r.score)))

                result_lists[name] = normalized_results

            except Exception as e:
                # Log error but continue with other sources
                print(f"Warning: Source {name} failed: {e}")

        if not result_lists:
            return []

        # Fuse results
        return self.fusion.fuse(result_lists, k=k)

    def search_with_weights(
        self,
        query: Any,
        weights: Dict[str, float],
        k: int = 10,
    ) -> List[FusedResult]:
        """
        Search with custom weights (for LinearFusion).

        Args:
            query: Search query
            weights: Source weights
            k: Number of results

        Returns:
            Fused results
        """
        # Temporarily override fusion strategy
        original_fusion = self.fusion
        self.fusion = LinearFusion(weights=weights)

        try:
            return self.search(query, k=k)
        finally:
            self.fusion = original_fusion

    def explain_result(self, result: FusedResult) -> Dict[str, Any]:
        """
        Get explanation for a fused result.

        Args:
            result: Fused result

        Returns:
            Explanation dict
        """
        return {
            "id": result.id,
            "final_score": result.score,
            "final_rank": result.rank,
            "source_contributions": [
                {
                    "source": source,
                    "score": result.source_scores.get(source, 0.0),
                    "rank": result.source_ranks.get(source, -1),
                    "weight": self._sources[source][1] if source in self._sources else 0.0,
                }
                for source in result.source_scores
            ],
        }

    def list_sources(self) -> List[Dict[str, Any]]:
        """List registered search sources."""
        return [
            {"name": name, "weight": weight}
            for name, (_, weight) in self._sources.items()
        ]


class Reranker:
    """
    Reranking utilities for search results.

    Includes MMR diversity reranking and cross-encoder reranking.
    """

    @staticmethod
    def mmr_rerank(
        results: List[FusedResult],
        vectors: Dict[str, np.ndarray],
        lambda_param: float = 0.5,
        k: int = 10,
    ) -> List[FusedResult]:
        """
        Maximal Marginal Relevance reranking for diversity.

        Args:
            results: Search results
            vectors: Dict mapping ID to vector
            lambda_param: Trade-off between relevance and diversity (0-1)
            k: Number of results

        Returns:
            Reranked results
        """
        if not results or not vectors:
            return results[:k]

        # Get vectors for results
        result_vectors = []
        valid_results = []

        for r in results:
            if r.id in vectors:
                result_vectors.append(vectors[r.id])
                valid_results.append(r)

        if not valid_results:
            return results[:k]

        vectors_array = np.array(result_vectors)

        # Normalize
        norms = np.linalg.norm(vectors_array, axis=1, keepdims=True)
        vectors_norm = vectors_array / (norms + 1e-8)

        # MMR selection
        selected = []
        remaining = list(range(len(valid_results)))

        while len(selected) < k and remaining:
            best_idx = None
            best_score = float('-inf')

            for idx in remaining:
                # Relevance (original score)
                relevance = valid_results[idx].score

                # Diversity (max similarity to selected)
                diversity = 0.0
                if selected:
                    selected_vectors = vectors_norm[selected]
                    similarities = np.dot(selected_vectors, vectors_norm[idx])
                    diversity = np.max(similarities)

                # MMR score
                mmr_score = lambda_param * relevance - (1 - lambda_param) * diversity

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx

            if best_idx is not None:
                selected.append(best_idx)
                remaining.remove(best_idx)

        # Build reranked results
        reranked = []
        for new_rank, idx in enumerate(selected, 1):
            result = valid_results[idx]
            reranked.append(FusedResult(
                id=result.id,
                score=result.score,  # Keep original score
                rank=new_rank,
                source_scores=result.source_scores,
                source_ranks=result.source_ranks,
                payload=result.payload,
            ))

        return reranked

    @staticmethod
    def score_threshold(
        results: List[FusedResult],
        min_score: float,
    ) -> List[FusedResult]:
        """
        Filter results below a score threshold.

        Args:
            results: Search results
            min_score: Minimum score to keep

        Returns:
            Filtered results
        """
        return [r for r in results if r.score >= min_score]

    @staticmethod
    def deduplicate(
        results: List[FusedResult],
        similarity_threshold: float = 0.95,
        vectors: Optional[Dict[str, np.ndarray]] = None,
    ) -> List[FusedResult]:
        """
        Remove near-duplicate results.

        Args:
            results: Search results
            similarity_threshold: Similarity threshold for duplicates
            vectors: Optional vectors for similarity computation

        Returns:
            Deduplicated results
        """
        if not vectors or len(results) <= 1:
            return results

        keep = []

        for result in results:
            if result.id not in vectors:
                keep.append(result)
                continue

            is_duplicate = False
            vec = vectors[result.id]
            vec_norm = vec / (np.linalg.norm(vec) + 1e-8)

            for kept in keep:
                if kept.id not in vectors:
                    continue

                kept_vec = vectors[kept.id]
                kept_norm = kept_vec / (np.linalg.norm(kept_vec) + 1e-8)

                similarity = np.dot(vec_norm, kept_norm)

                if similarity >= similarity_threshold:
                    is_duplicate = True
                    break

            if not is_duplicate:
                keep.append(result)

        # Update ranks
        for i, result in enumerate(keep, 1):
            result.rank = i

        return keep
