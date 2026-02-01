"""
VectrixDB Native HNSW Module

Pure Python/NumPy implementation of Hierarchical Navigable Small World graphs
for approximate nearest neighbor search. No external dependencies required.

Features:
- Configurable M (connections), ef_construction, ef_search
- Multiple distance metrics (cosine, euclidean, dot, manhattan)
- Memory-mapped persistence for large indexes
- Incremental add/remove operations
- Thread-safe operations
"""

from .distance import DistanceFunctions, DistanceMetric
from .index import NativeHNSWIndex

__all__ = [
    "DistanceFunctions",
    "DistanceMetric",
    "NativeHNSWIndex",
]
