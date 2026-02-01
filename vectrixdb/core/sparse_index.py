"""
VectrixDB Sparse Index - Efficient sparse vector search.

Implements inverted index for fast sparse vector similarity search,
similar to Qdrant's sparse vector support.

Features:
- Inverted index for O(k) dot product computation
- Support for SPLADE, BM25, and custom sparse embeddings
- Efficient top-k retrieval using heap
- Memory-efficient storage

Author: Daddy Nyame Owusu - Boakye
"""

import heapq
import json
import pickle
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from .types import SparseVector


@dataclass
class SparseSearchResult:
    """Result from sparse vector search."""
    id: str
    score: float


class SparseIndex:
    """
    Inverted index for efficient sparse vector similarity search.

    Uses an inverted index structure where each dimension maps to the
    documents that have non-zero values in that dimension.

    Supports:
    - Dot product similarity (default)
    - Cosine similarity (with normalized vectors)
    - Max-sim aggregation for multi-vector queries

    Example:
        >>> index = SparseIndex()
        >>> index.add("doc1", SparseVector.from_dict({0: 0.5, 10: 1.2}))
        >>> index.add("doc2", SparseVector.from_dict({0: 0.3, 20: 0.8}))
        >>> results = index.search(SparseVector.from_dict({0: 1.0, 10: 0.5}), limit=10)
    """

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        normalize: bool = False,
    ):
        """
        Initialize sparse index.

        Args:
            path: Optional path for persistence (SQLite-backed)
            normalize: Whether to L2 normalize vectors on add
        """
        self.path = Path(path) if path else None
        self.normalize = normalize
        self._lock = threading.RLock()

        # In-memory inverted index: dimension -> [(doc_id, value), ...]
        self._inverted_index: Dict[int, List[Tuple[str, float]]] = defaultdict(list)

        # Document storage: doc_id -> SparseVector
        self._docs: Dict[str, SparseVector] = {}

        # Document norms for cosine similarity
        self._norms: Dict[str, float] = {}

        # Statistics
        self._count = 0
        self._total_nnz = 0  # Total non-zero elements

        # Load from disk if exists
        if self.path and self.path.exists():
            self._load()

    def add(
        self,
        doc_id: str,
        sparse_vector: Union[SparseVector, Dict[int, float]],
    ) -> None:
        """
        Add a sparse vector to the index.

        Args:
            doc_id: Document identifier
            sparse_vector: Sparse vector (SparseVector or dict)
        """
        if isinstance(sparse_vector, dict):
            sparse_vector = SparseVector.from_dict(sparse_vector)

        if self.normalize:
            sparse_vector = sparse_vector.normalize()

        with self._lock:
            # Remove old version if exists
            if doc_id in self._docs:
                self._remove_from_index(doc_id)

            # Store document
            self._docs[doc_id] = sparse_vector
            self._norms[doc_id] = sparse_vector.norm()

            # Add to inverted index
            for idx, val in zip(sparse_vector.indices, sparse_vector.values):
                self._inverted_index[idx].append((doc_id, val))

            self._count += 1
            self._total_nnz += len(sparse_vector)

    def add_batch(
        self,
        doc_ids: List[str],
        sparse_vectors: List[Union[SparseVector, Dict[int, float]]],
    ) -> int:
        """
        Add multiple sparse vectors to the index.

        Args:
            doc_ids: List of document identifiers
            sparse_vectors: List of sparse vectors

        Returns:
            Number of vectors added
        """
        if len(doc_ids) != len(sparse_vectors):
            raise ValueError("doc_ids and sparse_vectors must have same length")

        added = 0
        for doc_id, sparse in zip(doc_ids, sparse_vectors):
            self.add(doc_id, sparse)
            added += 1

        return added

    def remove(self, doc_id: str) -> bool:
        """
        Remove a document from the index.

        Args:
            doc_id: Document identifier

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if doc_id not in self._docs:
                return False

            self._remove_from_index(doc_id)
            return True

    def _remove_from_index(self, doc_id: str) -> None:
        """Remove document from inverted index."""
        if doc_id not in self._docs:
            return

        sparse = self._docs[doc_id]

        # Remove from inverted index
        for idx in sparse.indices:
            self._inverted_index[idx] = [
                (d, v) for d, v in self._inverted_index[idx] if d != doc_id
            ]
            # Clean up empty lists
            if not self._inverted_index[idx]:
                del self._inverted_index[idx]

        self._total_nnz -= len(sparse)
        self._count -= 1

        del self._docs[doc_id]
        del self._norms[doc_id]

    def search(
        self,
        query: Union[SparseVector, Dict[int, float]],
        limit: int = 10,
        doc_ids: Optional[set] = None,
        score_threshold: Optional[float] = None,
    ) -> List[SparseSearchResult]:
        """
        Search for similar documents using sparse dot product.

        Args:
            query: Query sparse vector
            limit: Maximum results to return
            doc_ids: Optional set of doc IDs to search within
            score_threshold: Minimum score threshold

        Returns:
            List of SparseSearchResult sorted by score descending
        """
        if isinstance(query, dict):
            query = SparseVector.from_dict(query)

        if self.normalize:
            query = query.normalize()

        with self._lock:
            # Accumulate scores using inverted index
            scores: Dict[str, float] = defaultdict(float)

            for q_idx, q_val in zip(query.indices, query.values):
                if q_idx in self._inverted_index:
                    for doc_id, doc_val in self._inverted_index[q_idx]:
                        # Filter by doc_ids if provided
                        if doc_ids is not None and doc_id not in doc_ids:
                            continue
                        scores[doc_id] += q_val * doc_val

            # Apply score threshold
            if score_threshold is not None:
                scores = {d: s for d, s in scores.items() if s >= score_threshold}

            # Get top-k using heap
            if len(scores) <= limit:
                top_k = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            else:
                top_k = heapq.nlargest(limit, scores.items(), key=lambda x: x[1])

            return [
                SparseSearchResult(id=doc_id, score=score)
                for doc_id, score in top_k
            ]

    def search_cosine(
        self,
        query: Union[SparseVector, Dict[int, float]],
        limit: int = 10,
        doc_ids: Optional[set] = None,
    ) -> List[SparseSearchResult]:
        """
        Search using cosine similarity.

        Args:
            query: Query sparse vector
            limit: Maximum results
            doc_ids: Optional filter

        Returns:
            List of results sorted by cosine similarity
        """
        if isinstance(query, dict):
            query = SparseVector.from_dict(query)

        query_norm = query.norm()
        if query_norm == 0:
            return []

        # Get dot products
        dot_results = self.search(query, limit=limit * 2, doc_ids=doc_ids)

        # Normalize by document norms
        cosine_results = []
        for result in dot_results:
            doc_norm = self._norms.get(result.id, 1.0)
            if doc_norm > 0:
                cosine_score = result.score / (query_norm * doc_norm)
                cosine_results.append(SparseSearchResult(id=result.id, score=cosine_score))

        # Re-sort and limit
        cosine_results.sort(key=lambda x: x.score, reverse=True)
        return cosine_results[:limit]

    def get(self, doc_id: str) -> Optional[SparseVector]:
        """Get sparse vector by document ID."""
        return self._docs.get(doc_id)

    def count(self) -> int:
        """Number of documents in index."""
        return self._count

    def stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        return {
            "count": self._count,
            "total_nnz": self._total_nnz,
            "avg_nnz": self._total_nnz / self._count if self._count > 0 else 0,
            "vocab_size": len(self._inverted_index),
            "memory_estimate_mb": self._estimate_memory() / (1024 * 1024),
        }

    def _estimate_memory(self) -> int:
        """Estimate memory usage in bytes."""
        # Rough estimate: 8 bytes per float, 50 bytes per string on average
        docs_memory = sum(
            50 + len(sv.indices) * 12  # 4 bytes int + 8 bytes float
            for sv in self._docs.values()
        )
        index_memory = sum(
            len(posting) * 58  # 50 bytes string + 8 bytes float
            for posting in self._inverted_index.values()
        )
        return docs_memory + index_memory

    def save(self) -> None:
        """Save index to disk."""
        if not self.path:
            return

        self.path.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # Save using pickle for efficiency
            data = {
                "inverted_index": dict(self._inverted_index),
                "docs": {
                    doc_id: {"indices": sv.indices, "values": sv.values}
                    for doc_id, sv in self._docs.items()
                },
                "norms": self._norms,
                "count": self._count,
                "total_nnz": self._total_nnz,
                "normalize": self.normalize,
            }

            with open(self.path / "sparse_index.pkl", "wb") as f:
                pickle.dump(data, f)

    def _load(self) -> None:
        """Load index from disk."""
        pkl_path = self.path / "sparse_index.pkl"
        if not pkl_path.exists():
            return

        with open(pkl_path, "rb") as f:
            data = pickle.load(f)

        self._inverted_index = defaultdict(list, data["inverted_index"])
        self._docs = {
            doc_id: SparseVector(indices=sv["indices"], values=sv["values"])
            for doc_id, sv in data["docs"].items()
        }
        self._norms = data["norms"]
        self._count = data["count"]
        self._total_nnz = data["total_nnz"]
        self.normalize = data.get("normalize", False)

    def clear(self) -> None:
        """Clear all data from index."""
        with self._lock:
            self._inverted_index.clear()
            self._docs.clear()
            self._norms.clear()
            self._count = 0
            self._total_nnz = 0


class HybridSparseIndex:
    """
    Combined dense + sparse index for Qdrant-style hybrid search.

    Supports searching with both dense and sparse vectors simultaneously,
    combining results using Reciprocal Rank Fusion (RRF).

    Example:
        >>> hybrid = HybridSparseIndex(dimension=384)
        >>> hybrid.add(
        ...     "doc1",
        ...     dense=[0.1, 0.2, ...],
        ...     sparse=SparseVector.from_dict({0: 0.5, 10: 1.2})
        ... )
        >>> results = hybrid.search(
        ...     dense_query=[0.1, 0.2, ...],
        ...     sparse_query=SparseVector.from_dict({0: 1.0}),
        ...     dense_weight=0.7,
        ...     sparse_weight=0.3
        ... )
    """

    def __init__(
        self,
        dense_index: Any,  # HNSW or similar
        sparse_index: Optional[SparseIndex] = None,
        rrf_k: int = 60,
    ):
        """
        Initialize hybrid index.

        Args:
            dense_index: Dense vector index (HNSW)
            sparse_index: Sparse vector index
            rrf_k: RRF constant (default 60)
        """
        self.dense_index = dense_index
        self.sparse_index = sparse_index or SparseIndex()
        self.rrf_k = rrf_k

    def search(
        self,
        dense_query: Optional[Union[List[float], np.ndarray]] = None,
        sparse_query: Optional[Union[SparseVector, Dict[int, float]]] = None,
        limit: int = 10,
        dense_weight: float = 0.5,
        sparse_weight: float = 0.5,
        filter_ids: Optional[set] = None,
    ) -> List[Tuple[str, float, float, float]]:
        """
        Search using both dense and sparse vectors.

        Args:
            dense_query: Dense query vector
            sparse_query: Sparse query vector
            limit: Maximum results
            dense_weight: Weight for dense scores
            sparse_weight: Weight for sparse scores
            filter_ids: Optional set of IDs to search within

        Returns:
            List of (id, combined_score, dense_score, sparse_score)
        """
        dense_results = {}
        sparse_results = {}

        # Dense search
        if dense_query is not None and self.dense_index is not None:
            # Assumes dense_index has search method returning (ids, scores)
            dense_matches = self._dense_search(dense_query, limit * 3, filter_ids)
            dense_results = {r[0]: r[1] for r in dense_matches}

        # Sparse search
        if sparse_query is not None and self.sparse_index is not None:
            sparse_matches = self.sparse_index.search(
                sparse_query, limit=limit * 3, doc_ids=filter_ids
            )
            sparse_results = {r.id: r.score for r in sparse_matches}

        # Combine using RRF
        all_ids = set(dense_results.keys()) | set(sparse_results.keys())

        combined = []
        for doc_id in all_ids:
            dense_score = dense_results.get(doc_id, 0.0)
            sparse_score = sparse_results.get(doc_id, 0.0)

            # RRF fusion
            dense_rank = self._get_rank(doc_id, dense_results) if dense_score > 0 else float('inf')
            sparse_rank = self._get_rank(doc_id, sparse_results) if sparse_score > 0 else float('inf')

            rrf_score = 0.0
            if dense_rank < float('inf'):
                rrf_score += dense_weight / (self.rrf_k + dense_rank)
            if sparse_rank < float('inf'):
                rrf_score += sparse_weight / (self.rrf_k + sparse_rank)

            combined.append((doc_id, rrf_score, dense_score, sparse_score))

        # Sort by combined score
        combined.sort(key=lambda x: x[1], reverse=True)
        return combined[:limit]

    def _dense_search(
        self,
        query: Union[List[float], np.ndarray],
        limit: int,
        filter_ids: Optional[set],
    ) -> List[Tuple[str, float]]:
        """Search dense index (override for specific implementation)."""
        # This is a placeholder - actual implementation depends on dense index type
        return []

    def _get_rank(self, doc_id: str, scores: Dict[str, float]) -> int:
        """Get rank of document in sorted scores."""
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        try:
            return sorted_ids.index(doc_id) + 1
        except ValueError:
            return float('inf')
