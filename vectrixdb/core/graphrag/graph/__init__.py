"""
Knowledge Graph Module for VectrixDB GraphRAG.

Provides the core graph data structures and persistence:
- KnowledgeGraph: In-memory graph with entity deduplication
- GraphStorage: SQLite persistence layer
- SubGraph: Extracted graph subsets

Example:
    >>> from vectrixdb.core.graphrag.graph import KnowledgeGraph, GraphStorage
    >>>
    >>> graph = KnowledgeGraph()
    >>> graph.add_entity(Entity.create("Apple", "ORGANIZATION"))
    >>>
    >>> storage = GraphStorage("./graph.db")
    >>> storage.save_graph(graph)
"""

from .knowledge_graph import KnowledgeGraph, SubGraph
from .storage import GraphStorage
from .community import Community, CommunityHierarchy, CommunityDetector, detect_communities

__all__ = [
    "KnowledgeGraph",
    "SubGraph",
    "GraphStorage",
    "Community",
    "CommunityHierarchy",
    "CommunityDetector",
    "detect_communities",
]
