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

Author: Kwadwo Daddy Nyame Owusu - Boakye
License: Apache 2.0
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, Optional, TYPE_CHECKING, Tuple

__author__ = "Kwadwo Daddy Nyame Owusu - Boakye"
__tagline__ = "Where vectors come alive"


# ============================================================================
# THE TABLES: what is imported lazily, what is optional, and the version
# ============================================================================
#
# Nothing under the package is imported until asked for, so import vectrixdb
# is nearly free. The lazy table says which module holds each name, the
# optional table which extra an absent dependency belongs to, and the fallback
# is the version when the package is not installed.
# Every public name resolves on first access. `import vectrixdb` used to pull in
# numpy, usearch, asyncio, the storage backends and GraphRAG before a single
# call was made: 585 ms warm, 1.2 s cold. A library should cost what you use.

_LAZY: Dict[str, Tuple[str, Optional[str]]] = {
    "VectrixError": (".exceptions", "VectrixError"),
    "ConfigurationError": (".exceptions", "ConfigurationError"),
    "DependencyError": (".exceptions", "DependencyError"),
    "StorageError": (".exceptions", "StorageError"),
    "StorageConnectionError": (".exceptions", "StorageConnectionError"),
    "StorageOperationError": (".exceptions", "StorageOperationError"),
    "CollectionNotFoundError": (".exceptions", "CollectionNotFoundError"),
    "CollectionAlreadyExistsError": (".exceptions", "CollectionAlreadyExistsError"),
    "DocumentNotFoundError": (".exceptions", "DocumentNotFoundError"),
    "DimensionMismatchError": (".exceptions", "DimensionMismatchError"),
    "SearchError": (".exceptions", "SearchError"),
    "GraphUnavailable": (".exceptions", "GraphUnavailable"),
    "QuantizationError": (".exceptions", "QuantizationError"),
    "IndexBuildError": (".exceptions", "IndexBuildError"),
    "ModelError": (".exceptions", "ModelError"),
    "ModelNotFoundError": (".exceptions", "ModelNotFoundError"),
    "ModelDownloadError": (".exceptions", "ModelDownloadError"),
    "Vectrix": (".easy", "Vectrix"),
    "connect": (".client", "connect"),
    "VectrixClient": (".client", "VectrixClient"),
    "AsyncVectrixClient": (".client", "AsyncVectrixClient"),
    "Result": (".easy", "Result"),
    "Results": (".easy", "Results"),
    "RevocationReport": (".easy", "RevocationReport"),
    "create": (".easy", "create"),
    "open": (".easy", "open"),
    "quick_search": (".easy", "quick_search"),
    "V": (".easy", "Vectrix"),
    "VectrixDB": (".core.database", "VectrixDB"),
    "Collection": (".core.collection", "Collection"),
    "DistanceMetric": (".core.types", "DistanceMetric"),
    "SearchResult": (".core.types", "SearchResult"),
    "SearchResults": (".core.types", "SearchResults"),
    "SearchMode": (".core.types", "SearchMode"),
    "SearchQuery": (".core.types", "SearchQuery"),
    "Point": (".core.types", "Point"),
    "CollectionInfo": (".core.types", "CollectionInfo"),
    "DatabaseInfo": (".core.types", "DatabaseInfo"),
    "IndexConfig": (".core.types", "IndexConfig"),
    "IndexType": (".core.types", "IndexType"),
    "Filter": (".core.types", "Filter"),
    "FilterCondition": (".core.types", "FilterCondition"),
    "BatchResult": (".core.types", "BatchResult"),
    "SparseVector": (".core.types", "SparseVector"),
    "SparseIndex": (".core.sparse_index", "SparseIndex"),
    "Reranker": (".core.advanced_search", "Reranker"),
    "RerankConfig": (".core.advanced_search", "RerankConfig"),
    "RerankMethod": (".core.advanced_search", "RerankMethod"),
    "FacetAggregator": (".core.advanced_search", "FacetAggregator"),
    "FacetConfig": (".core.advanced_search", "FacetConfig"),
    "FacetResult": (".core.advanced_search", "FacetResult"),
    "ACLFilter": (".core.advanced_search", "ACLFilter"),
    "ACLPrincipal": (".core.advanced_search", "ACLPrincipal"),
    "TextAnalyzer": (".core.advanced_search", "TextAnalyzer"),
    "EnhancedSearchResults": (".core.advanced_search", "EnhancedSearchResults"),
    "StorageBackend": (".core.storage", "StorageBackend"),
    "StorageConfig": (".core.storage", "StorageConfig"),
    "BaseStorage": (".core.storage", "BaseStorage"),
    "InMemoryStorage": (".core.storage", "InMemoryStorage"),
    "SQLiteStorage": (".core.storage", "SQLiteStorage"),
    "LakebaseStorage": (".core.storage", "LakebaseStorage"),
    "DeltaLakeStorage": (".core.storage", "DeltaLakeStorage"),
    "OpenSearchStorage": (".core.storage", "OpenSearchStorage"),
    "AuroraPostgreSQLStorage": (".core.storage", "AuroraPostgreSQLStorage"),
    "create_storage": (".core.storage", "create_storage"),
    "CacheBackend": (".core.cache", "CacheBackend"),
    "CacheConfig": (".core.cache", "CacheConfig"),
    "BaseCache": (".core.cache", "BaseCache"),
    "MemoryCache": (".core.cache", "MemoryCache"),
    "VectorCache": (".core.cache", "VectorCache"),
    "create_cache": (".core.cache", "create_cache"),
    "ScalingStrategy": (".core.scaling", "ScalingStrategy"),
    "ScalingConfig": (".core.scaling", "ScalingConfig"),
    "AutoScaler": (".core.scaling", "AutoScaler"),
    "ResourceMonitor": (".core.scaling", "ResourceMonitor"),
    "MemoryManager": (".core.scaling", "MemoryManager"),
    "BaseQuantizer": (".core.quantization", "BaseQuantizer"),
    "ScalarQuantizer": (".core.quantization", "ScalarQuantizer"),
    "BinaryQuantizer": (".core.quantization", "BinaryQuantizer"),
    "ProductQuantizer": (".core.quantization", "ProductQuantizer"),
    "QuantizationConfig": (".core.quantization", "QuantizationConfig"),
    "QuantizationType": (".core.quantization", "QuantizationType"),
    "NativeHNSWIndex": (".core.hnsw", "NativeHNSWIndex"),
    "DistanceFunctions": (".core.hnsw", "DistanceFunctions"),
    "PayloadIndexManager": (".core.payload_index", "PayloadIndexManager"),
    "NumericRangeIndex": (".core.payload_index", "NumericRangeIndex"),
    "StringIndex": (".core.payload_index", "StringIndex"),
    "TagIndex": (".core.payload_index", "TagIndex"),
    "GeoIndex": (".core.payload_index", "GeoIndex"),
    "ParallelBatchProcessor": (".core.batch", "ParallelBatchProcessor"),
    "ParallelVectorInserter": (".core.batch", "ParallelVectorInserter"),
    "StreamingBatchProcessor": (".core.batch", "StreamingBatchProcessor"),
    "StreamingReader": (".core.batch", "StreamingReader"),
    "MemoryEfficientBatcher": (".core.batch", "MemoryEfficientBatcher"),
    "LargeDatasetProcessor": (".core.batch", "LargeDatasetProcessor"),
    "DenseSearch": (".core.search", "DenseSearch"),
    "MultiQuerySearch": (".core.search", "MultiQuerySearch"),
    "PrefetchRescore": (".core.search", "PrefetchRescore"),
    "SparseSearch": (".core.search", "SparseSearch"),
    "BM25Scorer": (".core.search", "BM25Scorer"),
    "QueryExpander": (".core.search", "QueryExpander"),
    "ColBERTSearch": (".core.search", "ColBERTSearch"),
    "MaxSimScorer": (".core.search", "MaxSimScorer"),
    "TokenEmbeddings": (".core.search", "TokenEmbeddings"),
    "EmbeddingManager": (".core.search", "EmbeddingManager"),
    "EmbeddingConfig": (".core.search", "EmbeddingConfig"),
    "FusionStrategy": (".core.search", "FusionStrategy"),
    "RRFFusion": (".core.search", "RRFFusion"),
    "LinearFusion": (".core.search", "LinearFusion"),
    "CondorcetFusion": (".core.search", "CondorcetFusion"),
    "HybridSearcher": (".core.search", "HybridSearcher"),
    "EmbeddedDenseProvider": (".core.search", "EmbeddedDenseProvider"),
    "EmbeddedSparseProvider": (".core.search", "EmbeddedSparseProvider"),
    "EmbeddedRerankerProvider": (".core.search", "EmbeddedRerankerProvider"),
    "get_embedded_provider": (".core.search", "get_embedded_provider"),
    "DenseEmbedder": (".models", "DenseEmbedder"),
    "SparseEmbedder": (".models", "SparseEmbedder"),
    "RerankerEmbedder": (".models", "RerankerEmbedder"),
    "LateInteractionEmbedder": (".models", "LateInteractionEmbedder"),
    "GraphExtractor": (".models", "GraphExtractor"),
    "Triplet": (".models", "Triplet"),
    "download_models": (".models", "download_models"),
    "is_models_installed": (".models", "is_models_installed"),
    "get_models_dir": (".models", "get_models_dir"),
    "BenchmarkRunner": (".benchmarks", "BenchmarkRunner"),
    "BenchmarkResult": (".benchmarks", "BenchmarkResult"),
    "BenchmarkDatasets": (".benchmarks", "BenchmarkDatasets"),
    "MetricsCollector": (".benchmarks", "MetricsCollector"),
    "BenchmarkReport": (".benchmarks", "BenchmarkReport"),
    "GraphRAGConfig": (".core.graphrag", "GraphRAGConfig"),
    "GraphRAGPipeline": (".core.graphrag", "GraphRAGPipeline"),
    "GraphRAGStats": (".core.graphrag", "GraphRAGStats"),
    "LLMProvider": (".core.graphrag", "LLMProvider"),
    "ExtractorType": (".core.graphrag", "ExtractorType"),
    "GraphSearchType": (".core.graphrag", "GraphSearchType"),
    "create_openai_config": (".core.graphrag", "create_openai_config"),
    "create_ollama_config": (".core.graphrag", "create_ollama_config"),
    "create_nlp_only_config": (".core.graphrag", "create_nlp_only_config"),
    "create_pipeline": (".core.graphrag", "create_pipeline"),
    "VectrixSync": (".core.sync", "VectrixSync"),
    "SyncResult": (".core.sync", "SyncResult"),
    "SyncStatus": (".core.sync", "SyncStatus"),
    "create_sync": (".core.sync", "create_sync"),
    "DocumentIndex": (".core.document_index", "DocumentIndex"),
    "DocumentInfo": (".core.document_index", "DocumentInfo"),
    "DocumentNode": (".core.document_index", "DocumentNode"),
    "DocumentType": (".core.document_index", "DocumentType"),
    "ChunkInfo": (".core.document_index", "ChunkInfo"),
    "chunk_text": (".core.document_index", "chunk_text"),
    "chunk": (".ingest", "chunk"),
    "load_document": (".ingest", "load"),
    "LoadedDocument": (".ingest", "LoadedDocument"),
    "AddReport": (".easy", "AddReport"),
    "RechunkPreview": (".easy", "RechunkPreview"),
    "tracing": (".tracing", None),
    "OpenAIEmbedder": (".models.openai_compat", "OpenAIEmbedder"),
    "plugins": (".plugins", None),
    "ModelMismatchWarning": (".exceptions", "ModelMismatchWarning"),
    "CollectionLoadWarning": (".exceptions", "CollectionLoadWarning"),
    "InvalidCollectionName": (".exceptions", "InvalidCollectionName"),
    "PolicyError": (".exceptions", "PolicyError"),
    "PolicyNotEnforcedWarning": (".exceptions", "PolicyNotEnforcedWarning"),
    "MetadataContractWarning": (".exceptions", "MetadataContractWarning"),
    "MetadataContractError": (".exceptions", "MetadataContractError"),
    "PolicyDefinitionError": (".exceptions", "PolicyDefinitionError"),
    "PrincipalRequired": (".exceptions", "PrincipalRequired"),
    "PrincipalIncomplete": (".exceptions", "PrincipalIncomplete"),
    "PolicyMismatch": (".exceptions", "PolicyMismatch"),
    "PushdownUnavailable": (".exceptions", "PushdownUnavailable"),
    "Policy": (".policy", "Policy"),
    "Overlap": (".policy", "Overlap"),
    "Equals": (".policy", "Equals"),
    "AtMost": (".policy", "AtMost"),
    "AtLeast": (".policy", "AtLeast"),
    "Excludes": (".policy", "Excludes"),
    "Present": (".policy", "Present"),
    "Decision": (".policy", "Decision"),
    "Outcome": (".policy", "Outcome"),
    "FilterPushdown": (".core.types", "FilterPushdown"),
    "AuditUnavailable": (".exceptions", "AuditUnavailable"),
    "AuditContext": (".audit", "AuditContext"),
    "AuditSink": (".audit", "AuditSink"),
    "RetrievalRecord": (".audit", "RetrievalRecord"),
    "IngestionRecord": (".audit", "IngestionRecord"),
    "JSONLSink": (".audit", "JSONLSink"),
    "PostgresAuditSink": (".audit", "PostgresAuditSink"),
    "MemorySink": (".audit", "MemorySink"),
    "Spool": (".audit", "Spool"),
    "DENY": (".audit", "DENY"),
    "ObjectLockSink": (".objectlock", "ObjectLockSink"),
    "S3ObjectLockStore": (".objectlock", "S3ObjectLockStore"),
    # Lineage
    "ChunkProvenance": (".lineage", "ChunkProvenance"),
    "Reproduction": (".lineage", "Reproduction"),
    "EvidencePack": (".lineage", "EvidencePack"),
    "write_evidence_pack": (".lineage", "write_evidence_pack"),
    # Extraction quality
    "ExtractionQuality": (".quality", "ExtractionQuality"),
    "extraction_quality": (".quality", "extraction_quality"),
    # Citations
    "CITATION_INSTRUCTION": (".citations", "CITATION_INSTRUCTION"),
    "Cited": (".citations", "Cited"),
    "citation_for": (".citations", "citation_for"),
    "parse_citations": (".citations", "parse_citations"),
    "readable_citation_for": (".citations", "readable_citation_for"),
    "validate_answer": (".citations", "validate_answer"),
    # Ingestion worker
    "BlobFetcher": (".worker", "BlobFetcher"),
    "IngestEvent": (".worker", "IngestEvent"),
    "IngestOutcome": (".worker", "IngestOutcome"),
    "IngestWorker": (".worker", "IngestWorker"),
    "LocalFetcher": (".worker", "LocalFetcher"),
    "LocalWatcher": (".worker", "LocalWatcher"),
    "S3Fetcher": (".worker", "S3Fetcher"),
    "events_from_event_grid": (".worker", "events_from_event_grid"),
    "events_from_s3": (".worker", "events_from_s3"),
    "extract": (".extract", None),
    "ExtractorRegistry": (".extract", "ExtractorRegistry"),
    "HttpExtractor": (".extract", "HttpExtractor"),
    "ExtractionError": (".exceptions", "ExtractionError"),
    "TranslationError": (".exceptions", "TranslationError"),
    "AzureTranslator": (".translate", "AzureTranslator"),
    "load_url": (".extract", "load_url"),
    "load_youtube": (".extract.youtube", "load_youtube"),
    "HttpDescriber": (".extract", "HttpDescriber"),
    "ChatDescriber": (".extract.describers", "ChatDescriber"),
    "DocumentStore": (".documents", "DocumentStore"),
    "ChunkStore": (".documents", "ChunkStore"),
    "LocalFiles": (".documents", "LocalFiles"),
    "S3Files": (".documents", "S3Files"),
    "BlobFiles": (".documents", "BlobFiles"),
    "ExtractionQualityError": (".exceptions", "ExtractionQualityError"),
    "ExtractionQualityWarning": (".exceptions", "ExtractionQualityWarning"),
    "SparseModelUnavailableWarning": (".exceptions", "SparseModelUnavailableWarning"),
    "chunk_with_context": (".core.document_index", "chunk_with_context"),
    "build_tree_from_markdown": (".core.document_index", "build_tree_from_markdown"),
    "build_tree_from_pdf": (".core.document_index", "build_tree_from_pdf"),
    "build_tree_from_text": (".core.document_index", "build_tree_from_text"),
    # Feeds and pages a collection keeps up with
    "Feed": (".sources", "Feed"),
    "Page": (".sources", "Page"),
    "Source": (".sources", "Source"),
    "Sources": (".sources", "Sources"),
    "register_source": (".sources", "register_source"),
}

# GraphRAG is optional; these come back as None when it cannot be imported,
# which is what the eager version did.
_OPTIONAL = frozenset(
    [
        "ExtractorType",
        "GraphRAGConfig",
        "GraphRAGPipeline",
        "GraphRAGStats",
        "GraphSearchType",
        "LLMProvider",
        "create_nlp_only_config",
        "create_ollama_config",
        "create_openai_config",
        "create_pipeline",
    ]
)

_VERSION_FALLBACK = "2.2.0"


# ============================================================================
# THE VERSION
# ============================================================================
#
# INPUT   the installed distribution's metadata
# OUTPUT  the version string, or the fallback when the package is not
#         installed
#
# Read once, the way pip records it.


def _version() -> str:
    # Single source of truth: the version declared in pyproject.toml. Reading
    # package metadata costs ~90 ms, so it is not paid at import time.
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("vectrixdb")
    except (ImportError, PackageNotFoundError):  # running from a source tree
        return _VERSION_FALLBACK


# ============================================================================
# LAZY ATTRIBUTES: the names, and the deprecated ones
# ============================================================================
#
# INPUT   a name asked of the package
# OUTPUT  the object from the module that holds it, imported on first use; a
#         deprecation warning and the new name for an old one; the names dir()
#         lists
#
# An optional extra that is not installed says which extra to install, rather
# than an ImportError from deep inside.
# Public names that still import but are on their way out. Each warns once
# per process on first access, stays for one minor release, and is removed
# in the next: 2.2 warns, 2.3 removes. The replacement is named in the
# warning so a caller can move without reading the changelog.

_DEPRECATED: Dict[str, str] = {
    "EmbeddingManager": "vectrixdb.models.DenseEmbedder, SparseEmbedder and RerankerEmbedder",
    "EmbeddingConfig": "the model arguments of vectrixdb.Vectrix",
    "EmbeddedDenseProvider": "vectrixdb.models.DenseEmbedder",
    "EmbeddedSparseProvider": "vectrixdb.models.SparseEmbedder",
    "EmbeddedRerankerProvider": "vectrixdb.models.RerankerEmbedder",
    "get_embedded_provider": "vectrixdb.models",
    "StreamingBatchProcessor": "Vectrix.add with an iterable, or vectrixdb.ParallelBatchProcessor",
    "StreamingReader": "the loaders in vectrixdb.ingest",
    # Exported, and called by nothing. Every search path filters with a linear
    # scan through Filter.matches, and the two have drifted into different
    # filter languages that disagree on five operator families. Nothing on the
    # roadmap asks for a pre-filtered index, so this goes rather than gets
    # reconciled.
    "PayloadIndexManager": "the filter argument of search(), which scans",
    "NumericRangeIndex": "the filter argument of search(), which scans",
    "StringIndex": "the filter argument of search(), which scans",
    "TagIndex": "the filter argument of search(), which scans",
    "GeoIndex": "the filter argument of search(), which scans",
}


def __getattr__(name: str) -> Any:
    if name in _DEPRECATED:
        import warnings

        warnings.warn(
            f"vectrixdb.{name} is deprecated since 2.2 and will be removed in 2.3; "
            f"use {_DEPRECATED[name]} instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    if name == "__version__":
        value: Any = _version()
    elif name == "GRAPHRAG_AVAILABLE":
        try:
            importlib.import_module(".core.graphrag", __name__)
            value = True
        except ImportError:
            value = False
    elif name in _LAZY:
        module, attr = _LAZY[name]
        try:
            loaded = importlib.import_module(module, __name__)
            value = loaded if attr is None else getattr(loaded, attr)
        except ImportError:
            if name not in _OPTIONAL:
                raise
            value = None
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value  # resolved once
    return value


def __dir__() -> list:
    return sorted(set(globals()) | set(_LAZY) | {"__version__", "GRAPHRAG_AVAILABLE"})


if TYPE_CHECKING:  # the real imports, for type checkers and IDEs only
    from .exceptions import (
        VectrixError,
        ConfigurationError,
        DependencyError,
        StorageError,
        StorageConnectionError,
        StorageOperationError,
        CollectionNotFoundError,
        CollectionAlreadyExistsError,
        DocumentNotFoundError,
        DimensionMismatchError,
        SearchError,
        GraphUnavailable,
        QuantizationError,
        IndexBuildError,
        ModelError,
        ModelNotFoundError,
        ModelDownloadError,
    )
    from .easy import Vectrix, Result, Results, create, open, quick_search
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
    from .core.sparse_index import SparseIndex
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
    from .core.storage import (
        StorageBackend,
        StorageConfig,
        BaseStorage,
        InMemoryStorage,
        SQLiteStorage,
        LakebaseStorage,
        DeltaLakeStorage,
        OpenSearchStorage,
        AuroraPostgreSQLStorage,
        create_storage,
    )
    from .core.cache import (
        CacheBackend,
        CacheConfig,
        BaseCache,
        MemoryCache,
        VectorCache,
        create_cache,
    )
    from .core.scaling import (
        ScalingStrategy,
        ScalingConfig,
        AutoScaler,
        ResourceMonitor,
        MemoryManager,
    )
    from .core.quantization import (
        BaseQuantizer,
        ScalarQuantizer,
        BinaryQuantizer,
        ProductQuantizer,
        QuantizationConfig,
        QuantizationType,
    )
    from .core.hnsw import (
        NativeHNSWIndex,
        DistanceFunctions,
    )
    from .core.payload_index import (
        PayloadIndexManager,
        NumericRangeIndex,
        StringIndex,
        TagIndex,
        GeoIndex,
    )
    from .core.batch import (
        ParallelBatchProcessor,
        ParallelVectorInserter,
        StreamingBatchProcessor,
        StreamingReader,
        MemoryEfficientBatcher,
        LargeDatasetProcessor,
    )
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
    from .benchmarks import (
        BenchmarkRunner,
        BenchmarkResult,
        BenchmarkDatasets,
        MetricsCollector,
        BenchmarkReport,
    )
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
    from .core.sync import (
        VectrixSync,
        SyncResult,
        SyncStatus,
        create_sync,
    )
    from .core.document_index import (
        DocumentIndex,
        DocumentInfo,
        DocumentNode,
        DocumentType,
        ChunkInfo,
        chunk_text,
        chunk_with_context,
        build_tree_from_markdown,
        build_tree_from_pdf,
        build_tree_from_text,
    )

    V = Vectrix


__all__ = [
    # Easy API (Recommended)
    "Vectrix",
    # A server, from Python
    "connect",
    "VectrixClient",
    "AsyncVectrixClient",
    "V",  # Backwards compatibility alias
    "Result",
    "Results",
    "RevocationReport",
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
    "LakebaseStorage",
    "DeltaLakeStorage",
    "OpenSearchStorage",
    "AuroraPostgreSQLStorage",
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
    # Sync
    "VectrixSync",
    "SyncResult",
    "SyncStatus",
    "create_sync",
    # Document Index
    "DocumentIndex",
    "DocumentInfo",
    "DocumentNode",
    "DocumentType",
    "ChunkInfo",
    "chunk_text",
    "chunk",
    "load_document",
    "LoadedDocument",
    "AddReport",
    "RechunkPreview",
    "tracing",
    "OpenAIEmbedder",
    "plugins",
    "ModelMismatchWarning",
    "CollectionLoadWarning",
    "InvalidCollectionName",
    # Entitlement policy
    "PolicyError",
    "PolicyNotEnforcedWarning",
    "MetadataContractWarning",
    "MetadataContractError",
    "PolicyDefinitionError",
    "PrincipalRequired",
    "PrincipalIncomplete",
    "PolicyMismatch",
    "PushdownUnavailable",
    "Policy",
    "Overlap",
    "Equals",
    "AtMost",
    "AtLeast",
    "Excludes",
    "Present",
    "Decision",
    "Outcome",
    "FilterPushdown",
    # Audit
    "AuditUnavailable",
    "AuditContext",
    "AuditSink",
    "RetrievalRecord",
    "IngestionRecord",
    "JSONLSink",
    "PostgresAuditSink",
    "MemorySink",
    "Spool",
    "DENY",
    "ObjectLockSink",
    "S3ObjectLockStore",
    # Lineage
    "ChunkProvenance",
    "Reproduction",
    "EvidencePack",
    "write_evidence_pack",
    "ExtractionQuality",
    "extraction_quality",
    "CITATION_INSTRUCTION",
    "Cited",
    "citation_for",
    "parse_citations",
    "readable_citation_for",
    "validate_answer",
    "BlobFetcher",
    "IngestEvent",
    "IngestOutcome",
    "IngestWorker",
    "LocalFetcher",
    "LocalWatcher",
    "S3Fetcher",
    "events_from_event_grid",
    "events_from_s3",
    "extract",
    "ExtractorRegistry",
    "HttpExtractor",
    "ExtractionError",
    "TranslationError",
    "AzureTranslator",
    "load_url",
    "load_youtube",
    "HttpDescriber",
    "ChatDescriber",
    "DocumentStore",
    "ChunkStore",
    "LocalFiles",
    "S3Files",
    "BlobFiles",
    "ExtractionQualityError",
    "ExtractionQualityWarning",
    "SparseModelUnavailableWarning",
    "chunk_with_context",
    "build_tree_from_markdown",
    "build_tree_from_pdf",
    "build_tree_from_text",
    "Feed",
    "Page",
    "Source",
    "Sources",
    "register_source",
    # Exceptions
    "VectrixError",
    "ConfigurationError",
    "DependencyError",
    "StorageError",
    "StorageConnectionError",
    "StorageOperationError",
    "CollectionNotFoundError",
    "CollectionAlreadyExistsError",
    "DocumentNotFoundError",
    "DimensionMismatchError",
    "SearchError",
    "GraphUnavailable",
    "QuantizationError",
    "IndexBuildError",
    "ModelError",
    "ModelNotFoundError",
    "ModelDownloadError",
    # Meta
    "__version__",
]
