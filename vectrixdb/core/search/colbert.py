"""
ColBERT-style Late Interaction Search

Token-level matching with MaxSim scoring for improved relevance.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import numpy as np


@dataclass
class TokenEmbeddings:
    """
    Token-level embeddings for ColBERT.

    Stores embeddings for each token in a text.
    """
    embeddings: np.ndarray  # Shape: (n_tokens, embedding_dim)
    tokens: Optional[List[str]] = None  # Original tokens (optional)
    mask: Optional[np.ndarray] = None  # Attention mask (optional)

    @property
    def n_tokens(self) -> int:
        """Number of tokens."""
        return len(self.embeddings)

    @property
    def dimension(self) -> int:
        """Embedding dimension."""
        return self.embeddings.shape[1] if len(self.embeddings.shape) > 1 else 0

    def normalize(self) -> "TokenEmbeddings":
        """Return normalized copy."""
        norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
        normalized = self.embeddings / (norms + 1e-8)
        return TokenEmbeddings(
            embeddings=normalized,
            tokens=self.tokens,
            mask=self.mask,
        )


@dataclass
class ColBERTResult:
    """ColBERT search result."""
    id: str
    score: float
    token_scores: Optional[List[float]] = None
    matched_tokens: Optional[List[Tuple[int, int, float]]] = None  # (query_idx, doc_idx, score)
    payload: Optional[Dict[str, Any]] = None


class MaxSimScorer:
    """
    MaxSim scoring for ColBERT-style late interaction.

    For each query token, finds the maximum similarity with any document token.
    Final score is the sum of these maximum similarities.

    Example:
        >>> scorer = MaxSimScorer()
        >>> score = scorer.score(query_embeddings, doc_embeddings)
    """

    def __init__(
        self,
        normalize: bool = True,
        use_mask: bool = True,
    ):
        """
        Initialize MaxSim scorer.

        Args:
            normalize: Normalize embeddings before scoring
            use_mask: Apply attention mask if available
        """
        self.normalize = normalize
        self.use_mask = use_mask

    def score(
        self,
        query: TokenEmbeddings,
        document: TokenEmbeddings,
        return_token_scores: bool = False,
    ) -> Tuple[float, Optional[List[float]]]:
        """
        Compute MaxSim score between query and document.

        Args:
            query: Query token embeddings
            document: Document token embeddings
            return_token_scores: Return per-token scores

        Returns:
            Tuple of (total_score, optional_token_scores)
        """
        q_emb = query.embeddings
        d_emb = document.embeddings

        # Normalize if requested
        if self.normalize:
            q_norms = np.linalg.norm(q_emb, axis=1, keepdims=True)
            d_norms = np.linalg.norm(d_emb, axis=1, keepdims=True)
            q_emb = q_emb / (q_norms + 1e-8)
            d_emb = d_emb / (d_norms + 1e-8)

        # Compute all pairwise similarities: (n_query_tokens, n_doc_tokens)
        similarities = np.dot(q_emb, d_emb.T)

        # Apply document mask if available
        if self.use_mask and document.mask is not None:
            # Mask out padding tokens (set to -inf)
            similarities[:, ~document.mask.astype(bool)] = float('-inf')

        # MaxSim: for each query token, take max over document tokens
        token_scores = similarities.max(axis=1).tolist()

        # Apply query mask if available
        if self.use_mask and query.mask is not None:
            token_scores = [
                score if mask else 0.0
                for score, mask in zip(token_scores, query.mask)
            ]

        total_score = sum(token_scores)

        return (total_score, token_scores if return_token_scores else None)

    def score_batch(
        self,
        query: TokenEmbeddings,
        documents: List[TokenEmbeddings],
    ) -> List[float]:
        """
        Score multiple documents against a query.

        Args:
            query: Query token embeddings
            documents: List of document token embeddings

        Returns:
            List of scores
        """
        return [self.score(query, doc)[0] for doc in documents]

    def get_token_matches(
        self,
        query: TokenEmbeddings,
        document: TokenEmbeddings,
        threshold: float = 0.5,
    ) -> List[Tuple[int, int, float]]:
        """
        Get matching token pairs above threshold.

        Args:
            query: Query embeddings
            document: Document embeddings
            threshold: Minimum similarity threshold

        Returns:
            List of (query_idx, doc_idx, similarity) tuples
        """
        q_emb = query.embeddings
        d_emb = document.embeddings

        if self.normalize:
            q_norms = np.linalg.norm(q_emb, axis=1, keepdims=True)
            d_norms = np.linalg.norm(d_emb, axis=1, keepdims=True)
            q_emb = q_emb / (q_norms + 1e-8)
            d_emb = d_emb / (d_norms + 1e-8)

        similarities = np.dot(q_emb, d_emb.T)

        matches = []
        for q_idx in range(len(q_emb)):
            for d_idx in range(len(d_emb)):
                sim = similarities[q_idx, d_idx]
                if sim >= threshold:
                    matches.append((q_idx, d_idx, float(sim)))

        return sorted(matches, key=lambda x: x[2], reverse=True)


class ColBERTSearch:
    """
    ColBERT-style late interaction search.

    Stores token embeddings for documents and performs MaxSim search.

    Example:
        >>> search = ColBERTSearch(dimension=128)
        >>> search.add("doc1", token_embeddings1)
        >>> results = search.search(query_embeddings, k=10)
    """

    def __init__(
        self,
        dimension: int = 128,
        normalize: bool = True,
    ):
        """
        Initialize ColBERT search.

        Args:
            dimension: Token embedding dimension
            normalize: Normalize embeddings
        """
        self.dimension = dimension
        self.normalize = normalize

        # Document storage
        self._documents: Dict[str, TokenEmbeddings] = {}

        # Scorer
        self._scorer = MaxSimScorer(normalize=normalize)

        # Optional: pre-computed token centroids for filtering
        self._centroids: Optional[np.ndarray] = None

    def add(self, id: str, embeddings: TokenEmbeddings) -> None:
        """
        Add a document's token embeddings.

        Args:
            id: Document ID
            embeddings: Token embeddings
        """
        if embeddings.dimension != self.dimension:
            raise ValueError(
                f"Embedding dimension {embeddings.dimension} != {self.dimension}"
            )

        self._documents[id] = embeddings

        # Invalidate centroids
        self._centroids = None

    def add_batch(self, items: List[Tuple[str, TokenEmbeddings]]) -> int:
        """
        Add multiple documents.

        Args:
            items: List of (id, embeddings) tuples

        Returns:
            Number added
        """
        for id_, emb in items:
            self.add(id_, emb)
        return len(items)

    def remove(self, id: str) -> bool:
        """Remove a document."""
        if id not in self._documents:
            return False
        del self._documents[id]
        self._centroids = None
        return True

    def search(
        self,
        query: TokenEmbeddings,
        k: int = 10,
        filter_ids: Optional[List[str]] = None,
        return_token_scores: bool = False,
    ) -> List[ColBERTResult]:
        """
        Search for similar documents.

        Args:
            query: Query token embeddings
            k: Number of results
            filter_ids: Only search these documents
            return_token_scores: Include per-token scores

        Returns:
            List of ColBERT results
        """
        # Determine documents to search
        if filter_ids:
            doc_ids = [id_ for id_ in filter_ids if id_ in self._documents]
        else:
            doc_ids = list(self._documents.keys())

        if not doc_ids:
            return []

        # Score all documents
        scored = []

        for doc_id in doc_ids:
            doc = self._documents[doc_id]
            score, token_scores = self._scorer.score(
                query, doc,
                return_token_scores=return_token_scores
            )

            scored.append(ColBERTResult(
                id=doc_id,
                score=score,
                token_scores=token_scores,
            ))

        # Sort by score
        scored.sort(key=lambda x: x.score, reverse=True)

        return scored[:k]

    def search_with_matches(
        self,
        query: TokenEmbeddings,
        k: int = 10,
        match_threshold: float = 0.5,
    ) -> List[ColBERTResult]:
        """
        Search and return token match details.

        Args:
            query: Query embeddings
            k: Number of results
            match_threshold: Minimum similarity for token matches

        Returns:
            Results with matched_tokens populated
        """
        results = self.search(query, k=k, return_token_scores=True)

        for result in results:
            doc = self._documents.get(result.id)
            if doc:
                result.matched_tokens = self._scorer.get_token_matches(
                    query, doc, threshold=match_threshold
                )

        return results

    def compute_centroids(self) -> np.ndarray:
        """
        Compute document centroids for fast filtering.

        Returns:
            Array of centroid vectors, shape (n_docs, dimension)
        """
        if self._centroids is not None:
            return self._centroids

        centroids = []
        for doc in self._documents.values():
            # Average of token embeddings
            centroid = np.mean(doc.embeddings, axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm
            centroids.append(centroid)

        self._centroids = np.array(centroids, dtype=np.float32)
        return self._centroids

    def search_with_prefetch(
        self,
        query: TokenEmbeddings,
        k: int = 10,
        prefetch_k: int = 100,
    ) -> List[ColBERTResult]:
        """
        Two-stage search: centroid prefetch + MaxSim rerank.

        Faster for large collections.

        Args:
            query: Query embeddings
            k: Final number of results
            prefetch_k: Number of candidates to prefetch

        Returns:
            Search results
        """
        if len(self._documents) <= prefetch_k:
            # Not worth prefetching
            return self.search(query, k=k)

        # Stage 1: Prefetch by centroid similarity
        centroids = self.compute_centroids()

        # Query centroid
        query_centroid = np.mean(query.embeddings, axis=0)
        query_norm = np.linalg.norm(query_centroid)
        if query_norm > 0:
            query_centroid = query_centroid / query_norm

        # Compute centroid similarities
        sims = np.dot(centroids, query_centroid)

        # Get top prefetch_k
        doc_ids = list(self._documents.keys())
        top_indices = np.argsort(-sims)[:prefetch_k]
        candidate_ids = [doc_ids[i] for i in top_indices]

        # Stage 2: Full MaxSim on candidates
        return self.search(query, k=k, filter_ids=candidate_ids)

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        total_tokens = sum(doc.n_tokens for doc in self._documents.values())

        return {
            "num_documents": len(self._documents),
            "total_tokens": total_tokens,
            "avg_tokens_per_doc": total_tokens / max(1, len(self._documents)),
            "dimension": self.dimension,
        }


class ColBERTEncoder:
    """
    Helper for encoding text to ColBERT token embeddings.

    This is a placeholder that uses random embeddings.
    In production, use a real ColBERT model.
    """

    def __init__(
        self,
        dimension: int = 128,
        max_tokens: int = 512,
    ):
        """
        Initialize encoder.

        Args:
            dimension: Output embedding dimension
            max_tokens: Maximum tokens
        """
        self.dimension = dimension
        self.max_tokens = max_tokens

    def encode(
        self,
        text: str,
        is_query: bool = False,
    ) -> TokenEmbeddings:
        """
        Encode text to token embeddings.

        NOTE: This is a placeholder using random embeddings.
        Replace with actual ColBERT model in production.

        Args:
            text: Input text
            is_query: Whether this is a query (affects processing)

        Returns:
            Token embeddings
        """
        # Simple tokenization (placeholder)
        tokens = text.lower().split()[:self.max_tokens]

        if not tokens:
            tokens = ["[PAD]"]

        # Random embeddings (placeholder - replace with real model)
        # Using hash-based seeding for reproducibility
        embeddings = []
        for token in tokens:
            seed = hash(token) % (2**32)
            rng = np.random.default_rng(seed)
            emb = rng.standard_normal(self.dimension).astype(np.float32)
            emb = emb / (np.linalg.norm(emb) + 1e-8)
            embeddings.append(emb)

        return TokenEmbeddings(
            embeddings=np.array(embeddings, dtype=np.float32),
            tokens=tokens,
            mask=np.ones(len(tokens), dtype=np.int32),
        )

    def encode_batch(
        self,
        texts: List[str],
        is_query: bool = False,
    ) -> List[TokenEmbeddings]:
        """Encode multiple texts."""
        return [self.encode(text, is_query) for text in texts]
