"""
VectrixDB GraphRAG Module.

Native GraphRAG implementation for VectrixDB - the first vector database
with built-in knowledge graph capabilities.

Features:
- Entity and relationship extraction (mREBEL bundled model - no LLM needed!)
- Hierarchical community detection (Leiden algorithm)
- Local, global, and hybrid (DRIFT) search strategies
- Incremental graph updates (LightRAG-style)
- Optional LLM support (OpenAI, Ollama, AWS, Azure)

Default: Uses mREBEL model for extraction - works offline, no API keys needed!

Example:
    >>> from vectrixdb import V
    >>> from vectrixdb.core.graphrag import GraphRAGConfig
    >>>
    >>> # Default: uses bundled mREBEL model (no LLM needed)
    >>> config = GraphRAGConfig(enabled=True)
    >>> db = V("knowledge_base", graphrag_config=config)
    >>> db.add(texts=documents)
    >>> results = db.search("What are the main themes?", mode="graph")
"""

from .config import (
    GraphRAGConfig,
    LLMProvider,
    ExtractorType,
    GraphSearchType,
    create_openai_config,
    create_ollama_config,
    create_nlp_only_config,
    create_rebel_config,
    create_default_config,
)

from .chunker import (
    TextUnit,
    DocumentChunker,
    create_chunker,
)

from .extractor.base import Entity, Relationship, ExtractionResult
from .extractor import HybridExtractor, REBELExtractor
from .extractor.nlp_extractor import NLPExtractor
from .extractor.llm_extractor import LLMExtractor

from .graph.knowledge_graph import KnowledgeGraph, SubGraph
from .graph.community import Community, CommunityHierarchy, CommunityDetector, detect_communities
from .graph.storage import GraphStorage

from .summarizer import CommunitySummarizer, summarize_communities

from .retriever import (
    LocalSearcher,
    LocalSearchResult,
    GlobalSearcher,
    GlobalSearchResult,
    HybridSearcher,
    GraphSearchResult,
)

from .pipeline import GraphRAGPipeline, GraphRAGStats, create_pipeline

__all__ = [
    # Configuration
    "GraphRAGConfig",
    "LLMProvider",
    "ExtractorType",
    "GraphSearchType",
    "create_openai_config",
    "create_ollama_config",
    "create_nlp_only_config",
    "create_rebel_config",
    "create_default_config",

    # Chunking
    "TextUnit",
    "DocumentChunker",
    "create_chunker",

    # Extraction
    "Entity",
    "Relationship",
    "ExtractionResult",
    "REBELExtractor",  # Default, recommended
    "HybridExtractor",
    "NLPExtractor",
    "LLMExtractor",

    # Graph
    "KnowledgeGraph",
    "SubGraph",
    "Community",
    "CommunityHierarchy",
    "CommunityDetector",
    "detect_communities",
    "GraphStorage",

    # Summarization
    "CommunitySummarizer",
    "summarize_communities",

    # Retrieval
    "LocalSearcher",
    "LocalSearchResult",
    "GlobalSearcher",
    "GlobalSearchResult",
    "HybridSearcher",
    "GraphSearchResult",

    # Pipeline
    "GraphRAGPipeline",
    "GraphRAGStats",
    "create_pipeline",
]

__version__ = "0.1.0"
