"""
Retrieval Engine for VectrixDB GraphRAG.

Provides local, global, and hybrid search capabilities:
- LocalSearcher: Entity-based search with graph traversal
- GlobalSearcher: Community-based search for broad queries
- HybridSearcher: DRIFT-style combined search (best of both)

Example:
    >>> from vectrixdb.core.graphrag.retriever import HybridSearcher
    >>>
    >>> searcher = HybridSearcher(graph, hierarchy, config)
    >>> results = searcher.search(query, query_vector)
"""

from .local_search import LocalSearcher, LocalSearchResult
from .global_search import GlobalSearcher, GlobalSearchResult
from .hybrid_search import HybridSearcher, GraphSearchResult

__all__ = [
    "LocalSearcher",
    "LocalSearchResult",
    "GlobalSearcher",
    "GlobalSearchResult",
    "HybridSearcher",
    "GraphSearchResult",
]
