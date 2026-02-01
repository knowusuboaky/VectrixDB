"""
VectrixDB Enhanced Search Module

Advanced search capabilities including:
- DenseSearch: Multi-query, negative queries, prefetch+rescore
- SparseSearch: BM25-style scoring, query expansion
- ColBERTSearch: Late interaction MaxSim search
- EmbeddingManager: Auto text-to-vector conversion
- FusionStrategies: RRF, linear combination, Condorcet
- Embedded Models: Offline embeddings with no network calls
"""

from .dense import DenseSearch, MultiQuerySearch, PrefetchRescore
from .sparse import SparseSearch, BM25Scorer, QueryExpander
from .colbert import ColBERTSearch, MaxSimScorer, TokenEmbeddings
from .embeddings import (
    EmbeddingManager,
    EmbeddingConfig,
    EmbeddedDenseProvider,
    EmbeddedSparseProvider,
    EmbeddedRerankerProvider,
    get_embedded_provider,
)
from .fusion import (
    FusionStrategy,
    RRFFusion,
    LinearFusion,
    CondorcetFusion,
    HybridSearcher,
)

__all__ = [
    # Dense search
    "DenseSearch",
    "MultiQuerySearch",
    "PrefetchRescore",
    # Sparse search
    "SparseSearch",
    "BM25Scorer",
    "QueryExpander",
    # ColBERT
    "ColBERTSearch",
    "MaxSimScorer",
    "TokenEmbeddings",
    # Embeddings
    "EmbeddingManager",
    "EmbeddingConfig",
    # Embedded models (no network calls)
    "EmbeddedDenseProvider",
    "EmbeddedSparseProvider",
    "EmbeddedRerankerProvider",
    "get_embedded_provider",
    # Fusion
    "FusionStrategy",
    "RRFFusion",
    "LinearFusion",
    "CondorcetFusion",
    "HybridSearcher",
]
