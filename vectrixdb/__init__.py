"""
VectrixDB - Where vectors come alive.

The simplest, most powerful vector database. Zero config. Text in, results out.

EASY API (Recommended):
    >>> from vectrixdb import Vectrix
    >>>
    >>> # Create and add - ONE LINE
    >>> db = Vectrix("my_docs").add(["Python is great", "ML is fun", "AI is the future"])
    >>>
    >>> # Search - ONE LINE
    >>> results = db.search("programming")
    >>> print(results.top.text)
    >>>
    >>> # Full power - STILL ONE LINE
    >>> results = db.search("artificial intelligence", mode="ultimate")

COMPARISON WITH COMPETITORS:

    # Chroma (4 lines)
    client = chromadb.Client()
    collection = client.create_collection("docs")
    collection.add(documents=["text"], ids=["1"])
    results = collection.query(query_texts=["query"])

    # Pinecone (5+ lines + API key + manual embedding)
    pinecone.init(api_key="...")
    index = pinecone.Index("docs")
    embedding = model.encode("text")
    index.upsert(vectors=[...])
    results = index.query(vector=embedding)

    # VectrixDB (1 line each!)
    db = Vectrix("docs").add(["text"])
    results = db.search("query")

ADVANCED API (Full Control):
    >>> from vectrixdb import VectrixDB
    >>> db = VectrixDB("./my_vectors")
    >>> collection = db.create_collection("documents", dimension=384)
    >>> collection.add(ids=["doc1"], vectors=[[0.1, 0.2, ...]])
    >>> results = collection.search(query=[0.1, 0.2, ...], limit=10)

Author: VectrixDB Team
License: Apache 2.0
"""

__version__ = "1.0.4"
__author__ = "VectrixDB Team"
__tagline__ = "Where vectors come alive"

# =============================================================================
# EASY API (Recommended for most users)
# =============================================================================
from .easy import Vectrix, Result, Results, create, open, quick_search

# Backwards compatibility alias
V = Vectrix

# =============================================================================
# ADVANCED API (Full control)
# =============================================================================
from .core.database import VectrixDB
from .core.collection import Collection
from .core.types import (
    DistanceMetric,
    SearchResult,
    SearchResults,
    SearchMode,
    SearchQuery,
    Point,
    CollectionInfo,
    DatabaseInfo,
    IndexConfig,
    IndexType,
    Filter,
    FilterCondition,
    BatchResult,
    SparseVector,
)

# Sparse vector index
from .core.sparse_index import SparseIndex

# Advanced search (Enterprise features)
from .core.advanced_search import (
    Reranker,
    RerankConfig,
    RerankMethod,
    FacetAggregator,
    FacetConfig,
    FacetResult,
    ACLFilter,
    ACLPrincipal,
    TextAnalyzer,
    EnhancedSearchResults,
)

# Storage backends
from .core.storage import (
    StorageBackend,
    StorageConfig,
    BaseStorage,
    InMemoryStorage,
    SQLiteStorage,
    create_storage,
)

# Caching layer
from .core.cache import (
    CacheBackend,
    CacheConfig,
    BaseCache,
    MemoryCache,
    VectorCache,
    create_cache,
)

# Auto-scaling
from .core.scaling import (
    ScalingStrategy,
    ScalingConfig,
    AutoScaler,
    ResourceMonitor,
    MemoryManager,
)

# Quantization
from .core.quantization import (
    BaseQuantizer,
    ScalarQuantizer,
    BinaryQuantizer,
    ProductQuantizer,
    QuantizationConfig,
    QuantizationType,
)

# Native HNSW
from .core.hnsw import (
    NativeHNSWIndex,
    DistanceFunctions,
)

# Payload Indexing
from .core.payload_index import (
    PayloadIndexManager,
    NumericRangeIndex,
    StringIndex,
    TagIndex,
    GeoIndex,
)

# Batch Operations
from .core.batch import (
    ParallelBatchProcessor,
    ParallelVectorInserter,
    StreamingBatchProcessor,
    StreamingReader,
    MemoryEfficientBatcher,
    LargeDatasetProcessor,
)

# Enhanced Search
from .core.search import (
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
    # Embedded models (no network calls)
    EmbeddedDenseProvider,
    EmbeddedSparseProvider,
    EmbeddedRerankerProvider,
    get_embedded_provider,
)

# Embedded Models (no network calls after setup)
from .models import (
    DenseEmbedder,
    SparseEmbedder,
    RerankerEmbedder,
    LateInteractionEmbedder,
    GraphExtractor,
    Triplet,
    download_models,
    is_models_installed,
    get_models_dir,
)

# Benchmarking
from .benchmarks import (
    BenchmarkRunner,
    BenchmarkResult,
    BenchmarkDatasets,
    MetricsCollector,
    BenchmarkReport,
)

# GraphRAG (optional - requires graphrag dependencies)
try:
    from .core.graphrag import (
        GraphRAGConfig,
        GraphRAGPipeline,
        GraphRAGStats,
        LLMProvider,
        ExtractorType,
        GraphSearchType,
        create_openai_config,
        create_ollama_config,
        create_nlp_only_config,
        create_pipeline,
    )
    GRAPHRAG_AVAILABLE = True
except ImportError:
    GRAPHRAG_AVAILABLE = False
    GraphRAGConfig = None
    GraphRAGPipeline = None

__all__ = [
    # Easy API (Recommended)
    "Vectrix",
    "V",  # Backwards compatibility alias
    "Result",
    "Results",
    "create",
    "open",
    "quick_search",
    # Advanced API
    "VectrixDB",
    "Collection",
    # Types
    "DistanceMetric",
    "SearchResult",
    "SearchResults",
    "SearchMode",
    "SearchQuery",
    "Point",
    "CollectionInfo",
    "DatabaseInfo",
    "IndexConfig",
    "IndexType",
    "Filter",
    "FilterCondition",
    "BatchResult",
    # Sparse Vectors
    "SparseVector",
    "SparseIndex",
    # Advanced Search (Enterprise)
    "Reranker",
    "RerankConfig",
    "RerankMethod",
    "FacetAggregator",
    "FacetConfig",
    "FacetResult",
    "ACLFilter",
    "ACLPrincipal",
    "TextAnalyzer",
    "EnhancedSearchResults",
    # Storage
    "StorageBackend",
    "StorageConfig",
    "BaseStorage",
    "InMemoryStorage",
    "SQLiteStorage",
    "create_storage",
    # Cache
    "CacheBackend",
    "CacheConfig",
    "BaseCache",
    "MemoryCache",
    "VectorCache",
    "create_cache",
    # Scaling
    "ScalingStrategy",
    "ScalingConfig",
    "AutoScaler",
    "ResourceMonitor",
    "MemoryManager",
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
    # Embedded Models (no network calls)
    "EmbeddedDenseProvider",
    "EmbeddedSparseProvider",
    "EmbeddedRerankerProvider",
    "get_embedded_provider",
    "DenseEmbedder",
    "SparseEmbedder",
    "RerankerEmbedder",
    "LateInteractionEmbedder",
    "GraphExtractor",
    "Triplet",
    "download_models",
    "is_models_installed",
    "get_models_dir",
    # Benchmarking
    "BenchmarkRunner",
    "BenchmarkResult",
    "BenchmarkDatasets",
    "MetricsCollector",
    "BenchmarkReport",
    # GraphRAG
    "GraphRAGConfig",
    "GraphRAGPipeline",
    "GraphRAGStats",
    "LLMProvider",
    "ExtractorType",
    "GraphSearchType",
    "create_openai_config",
    "create_ollama_config",
    "create_nlp_only_config",
    "create_pipeline",
    "GRAPHRAG_AVAILABLE",
    # Meta
    "__version__",
]

