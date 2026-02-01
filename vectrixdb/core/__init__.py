"""
VectrixDB Core - Vector engine and storage.

Author: Daddy Nyame Owusu - Boakye
"""

from .database import VectrixDB
from .collection import Collection
from .types import DistanceMetric, SearchResult, Point, CollectionInfo

# Quantization module
from .quantization import (
    BaseQuantizer,
    ScalarQuantizer,
    BinaryQuantizer,
    ProductQuantizer,
    QuantizationConfig,
    QuantizationType,
)

# Native HNSW module
from .hnsw import (
    NativeHNSWIndex,
    DistanceFunctions,
)

# Payload indexing module
from .payload_index import (
    PayloadIndexManager,
    NumericRangeIndex,
    StringIndex,
    TagIndex,
    GeoIndex,
)

# Batch operations module
from .batch import (
    ParallelBatchProcessor,
    ParallelVectorInserter,
    StreamingBatchProcessor,
    StreamingReader,
    MemoryEfficientBatcher,
    LargeDatasetProcessor,
)

# Enhanced search module
from .search import (
    DenseSearch,
    MultiQuerySearch,
    PrefetchRescore,
    SparseSearch,
    BM25Scorer,
    QueryExpander,
    ColBERTSearch,
    MaxSimScorer,
    TokenEmbeddings,
    EmbeddingManager,
    EmbeddingConfig,
    FusionStrategy,
    RRFFusion,
    LinearFusion,
    CondorcetFusion,
    HybridSearcher,
)

__all__ = [
    # Core
    "VectrixDB",
    "Collection",
    "DistanceMetric",
    "SearchResult",
    "Point",
    "CollectionInfo",
    # Quantization
    "BaseQuantizer",
    "ScalarQuantizer",
    "BinaryQuantizer",
    "ProductQuantizer",
    "QuantizationConfig",
    "QuantizationType",
    # Native HNSW
    "NativeHNSWIndex",
    "DistanceFunctions",
    # Payload Indexing
    "PayloadIndexManager",
    "NumericRangeIndex",
    "StringIndex",
    "TagIndex",
    "GeoIndex",
    # Batch Operations
    "ParallelBatchProcessor",
    "ParallelVectorInserter",
    "StreamingBatchProcessor",
    "StreamingReader",
    "MemoryEfficientBatcher",
    "LargeDatasetProcessor",
    # Enhanced Search
    "DenseSearch",
    "MultiQuerySearch",
    "PrefetchRescore",
    "SparseSearch",
    "BM25Scorer",
    "QueryExpander",
    "ColBERTSearch",
    "MaxSimScorer",
    "TokenEmbeddings",
    "EmbeddingManager",
    "EmbeddingConfig",
    "FusionStrategy",
    "RRFFusion",
    "LinearFusion",
    "CondorcetFusion",
    "HybridSearcher",
]
