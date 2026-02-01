"""
Sparse Vector Search

BM25-style scoring, sparse vector search, and query expansion.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
import numpy as np


@dataclass
class SparseVector:
    """Sparse vector representation."""
    indices: np.ndarray  # Non-zero indices
    values: np.ndarray   # Corresponding values
    dimension: int = 0   # Maximum dimension (optional)

    @classmethod
    def from_dict(cls, d: Dict[int, float], dimension: int = 0) -> "SparseVector":
        """Create from dictionary {index: value}."""
        if not d:
            return cls(
                indices=np.array([], dtype=np.int32),
                values=np.array([], dtype=np.float32),
                dimension=dimension,
            )

        indices = np.array(list(d.keys()), dtype=np.int32)
        values = np.array(list(d.values()), dtype=np.float32)

        # Sort by index
        order = np.argsort(indices)

        return cls(
            indices=indices[order],
            values=values[order],
            dimension=dimension or (int(indices.max()) + 1 if len(indices) > 0 else 0),
        )

    def to_dict(self) -> Dict[int, float]:
        """Convert to dictionary."""
        return {int(i): float(v) for i, v in zip(self.indices, self.values)}

    def dot(self, other: "SparseVector") -> float:
        """Compute dot product with another sparse vector."""
        # Use merge-based intersection
        i, j = 0, 0
        result = 0.0

        while i < len(self.indices) and j < len(other.indices):
            if self.indices[i] == other.indices[j]:
                result += self.values[i] * other.values[j]
                i += 1
                j += 1
            elif self.indices[i] < other.indices[j]:
                i += 1
            else:
                j += 1

        return result

    def norm(self) -> float:
        """Compute L2 norm."""
        return float(np.linalg.norm(self.values))


@dataclass
class SparseSearchResult:
    """Sparse search result."""
    id: str
    score: float
    matched_terms: int = 0
    payload: Optional[Dict[str, Any]] = None


class SparseSearch:
    """
    Sparse vector search with inverted index.

    Efficient for high-dimensional sparse vectors like TF-IDF or SPLADE.

    Example:
        >>> search = SparseSearch(dimension=30000)
        >>> search.add("doc1", sparse_vector1)
        >>> search.add("doc2", sparse_vector2)
        >>> results = search.search(query_sparse, k=10)
    """

    def __init__(self, dimension: int = 30000):
        """
        Initialize sparse search.

        Args:
            dimension: Maximum vector dimension
        """
        self.dimension = dimension

        # Inverted index: dimension -> [(doc_id, value)]
        self._inverted_index: Dict[int, List[Tuple[str, float]]] = defaultdict(list)

        # Document storage
        self._docs: Dict[str, SparseVector] = {}

        # Document norms for cosine similarity
        self._norms: Dict[str, float] = {}

    def add(self, id: str, vector: SparseVector) -> None:
        """
        Add a sparse vector.

        Args:
            id: Document ID
            vector: Sparse vector
        """
        # Remove old version if exists
        if id in self._docs:
            self.remove(id)

        # Store document
        self._docs[id] = vector
        self._norms[id] = vector.norm()

        # Update inverted index
        for idx, value in zip(vector.indices, vector.values):
            self._inverted_index[int(idx)].append((id, float(value)))

    def add_batch(self, items: List[Tuple[str, SparseVector]]) -> int:
        """
        Add multiple sparse vectors.

        Args:
            items: List of (id, vector) tuples

        Returns:
            Number of vectors added
        """
        for id_, vector in items:
            self.add(id_, vector)
        return len(items)

    def remove(self, id: str) -> bool:
        """
        Remove a document.

        Args:
            id: Document ID

        Returns:
            True if removed, False if not found
        """
        if id not in self._docs:
            return False

        vector = self._docs[id]

        # Remove from inverted index
        for idx in vector.indices:
            self._inverted_index[int(idx)] = [
                (doc_id, val)
                for doc_id, val in self._inverted_index[int(idx)]
                if doc_id != id
            ]

        del self._docs[id]
        del self._norms[id]

        return True

    def search(
        self,
        query: SparseVector,
        k: int = 10,
        filter_ids: Optional[Set[str]] = None,
        metric: str = "dot",
    ) -> List[SparseSearchResult]:
        """
        Search for similar sparse vectors.

        Args:
            query: Query sparse vector
            k: Number of results
            filter_ids: Only search within these IDs
            metric: Scoring metric (dot, cosine)

        Returns:
            List of search results
        """
        # Accumulate scores using inverted index
        scores: Dict[str, float] = defaultdict(float)
        matches: Dict[str, int] = defaultdict(int)

        for idx, query_value in zip(query.indices, query.values):
            idx = int(idx)

            if idx not in self._inverted_index:
                continue

            for doc_id, doc_value in self._inverted_index[idx]:
                if filter_ids and doc_id not in filter_ids:
                    continue

                scores[doc_id] += query_value * doc_value
                matches[doc_id] += 1

        # Apply normalization for cosine similarity
        if metric == "cosine":
            query_norm = query.norm()
            for doc_id in scores:
                if self._norms[doc_id] > 0 and query_norm > 0:
                    scores[doc_id] /= (self._norms[doc_id] * query_norm)

        # Sort by score
        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Return top k
        results = []
        for doc_id, score in sorted_docs[:k]:
            results.append(SparseSearchResult(
                id=doc_id,
                score=score,
                matched_terms=matches[doc_id],
            ))

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        return {
            "num_documents": len(self._docs),
            "num_posting_lists": len(self._inverted_index),
            "avg_posting_list_length": (
                sum(len(v) for v in self._inverted_index.values()) /
                max(1, len(self._inverted_index))
            ),
        }


class BM25Scorer:
    """
    BM25 scoring for text search.

    Implements Okapi BM25 with configurable k1 and b parameters.

    Example:
        >>> scorer = BM25Scorer()
        >>> scorer.add_document("doc1", ["hello", "world"])
        >>> scores = scorer.score("hello", k=10)
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Initialize BM25 scorer.

        Args:
            k1: Term frequency saturation parameter (typically 1.2-2.0)
            b: Length normalization parameter (0-1)
        """
        self.k1 = k1
        self.b = b

        # Document term frequencies
        self._doc_tf: Dict[str, Dict[str, int]] = {}

        # Document lengths
        self._doc_lengths: Dict[str, int] = {}

        # Document frequency (how many docs contain each term)
        self._df: Dict[str, int] = defaultdict(int)

        # Total documents
        self._total_docs = 0

        # Average document length
        self._avg_doc_length = 0.0

    def add_document(self, id: str, tokens: List[str]) -> None:
        """
        Add a document.

        Args:
            id: Document ID
            tokens: Document tokens (words)
        """
        # Remove old version if exists
        if id in self._doc_tf:
            self.remove_document(id)

        # Compute term frequencies
        tf: Dict[str, int] = defaultdict(int)
        for token in tokens:
            tf[token] += 1

        # Store document
        self._doc_tf[id] = dict(tf)
        self._doc_lengths[id] = len(tokens)

        # Update document frequencies
        for term in tf:
            self._df[term] += 1

        # Update stats
        self._total_docs += 1
        self._update_avg_length()

    def remove_document(self, id: str) -> bool:
        """Remove a document."""
        if id not in self._doc_tf:
            return False

        # Update document frequencies
        for term in self._doc_tf[id]:
            self._df[term] -= 1
            if self._df[term] <= 0:
                del self._df[term]

        del self._doc_tf[id]
        del self._doc_lengths[id]

        self._total_docs -= 1
        self._update_avg_length()

        return True

    def _update_avg_length(self) -> None:
        """Update average document length."""
        if self._total_docs > 0:
            self._avg_doc_length = sum(self._doc_lengths.values()) / self._total_docs
        else:
            self._avg_doc_length = 0.0

    def score(
        self,
        query_tokens: List[str],
        k: int = 10,
        filter_ids: Optional[Set[str]] = None,
    ) -> List[SparseSearchResult]:
        """
        Score documents for a query.

        Args:
            query_tokens: Query tokens
            k: Number of results
            filter_ids: Only score these documents

        Returns:
            Scored documents
        """
        scores: Dict[str, float] = defaultdict(float)
        matched: Dict[str, int] = defaultdict(int)

        # Compute IDF for query terms
        idf = {}
        for term in set(query_tokens):
            df = self._df.get(term, 0)
            if df > 0:
                # IDF with smoothing
                idf[term] = math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1)

        # Score each document
        docs_to_score = filter_ids or set(self._doc_tf.keys())

        for doc_id in docs_to_score:
            if doc_id not in self._doc_tf:
                continue

            doc_tf = self._doc_tf[doc_id]
            doc_len = self._doc_lengths[doc_id]

            for term in query_tokens:
                if term not in doc_tf or term not in idf:
                    continue

                tf = doc_tf[term]

                # BM25 scoring formula
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * (doc_len / self._avg_doc_length)
                )

                scores[doc_id] += idf[term] * (numerator / denominator)
                matched[doc_id] += 1

        # Sort by score
        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for doc_id, score in sorted_docs[:k]:
            results.append(SparseSearchResult(
                id=doc_id,
                score=score,
                matched_terms=matched[doc_id],
            ))

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get scorer statistics."""
        return {
            "total_documents": self._total_docs,
            "vocabulary_size": len(self._df),
            "avg_document_length": self._avg_doc_length,
        }


class QueryExpander:
    """
    Query expansion for improved recall.

    Supports synonym expansion, stemming, and relevance feedback.

    Example:
        >>> expander = QueryExpander()
        >>> expander.add_synonyms("quick", ["fast", "rapid", "swift"])
        >>> expanded = expander.expand(["quick", "brown", "fox"])
    """

    def __init__(self):
        """Initialize query expander."""
        # Synonym mapping
        self._synonyms: Dict[str, List[str]] = {}

        # Learned expansions from relevance feedback
        self._learned: Dict[str, List[Tuple[str, float]]] = {}

    def add_synonyms(self, term: str, synonyms: List[str]) -> None:
        """
        Add synonyms for a term.

        Args:
            term: Base term
            synonyms: List of synonyms
        """
        term = term.lower()
        if term not in self._synonyms:
            self._synonyms[term] = []

        for syn in synonyms:
            syn = syn.lower()
            if syn not in self._synonyms[term]:
                self._synonyms[term].append(syn)

    def add_learned_expansion(
        self,
        term: str,
        expansion: str,
        weight: float = 0.5,
    ) -> None:
        """
        Add learned expansion from relevance feedback.

        Args:
            term: Original term
            expansion: Expansion term
            weight: Expansion weight (0-1)
        """
        term = term.lower()
        if term not in self._learned:
            self._learned[term] = []

        self._learned[term].append((expansion.lower(), weight))

    def expand(
        self,
        tokens: List[str],
        use_synonyms: bool = True,
        use_learned: bool = True,
        max_expansions_per_term: int = 3,
    ) -> List[Tuple[str, float]]:
        """
        Expand query tokens.

        Args:
            tokens: Original query tokens
            use_synonyms: Include synonym expansions
            use_learned: Include learned expansions
            max_expansions_per_term: Maximum expansions per term

        Returns:
            List of (token, weight) tuples
        """
        expanded: List[Tuple[str, float]] = []

        for token in tokens:
            token = token.lower()

            # Original term with weight 1.0
            expanded.append((token, 1.0))

            expansions_added = 0

            # Add synonyms
            if use_synonyms and token in self._synonyms:
                for syn in self._synonyms[token][:max_expansions_per_term]:
                    expanded.append((syn, 0.7))  # Synonyms get lower weight
                    expansions_added += 1

                    if expansions_added >= max_expansions_per_term:
                        break

            # Add learned expansions
            if use_learned and token in self._learned:
                remaining = max_expansions_per_term - expansions_added
                for exp, weight in self._learned[token][:remaining]:
                    expanded.append((exp, weight))

        return expanded

    def expand_to_tokens(
        self,
        tokens: List[str],
        **kwargs,
    ) -> List[str]:
        """
        Expand and return flat list of tokens (ignoring weights).

        Args:
            tokens: Original tokens
            **kwargs: Arguments passed to expand()

        Returns:
            List of expanded tokens
        """
        expanded = self.expand(tokens, **kwargs)
        return [token for token, _ in expanded]
