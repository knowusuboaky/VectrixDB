"""
GraphRAG Configuration for VectrixDB.

Defines all configuration options for the GraphRAG module including
entity extraction, graph construction, community detection, and retrieval.
"""

from dataclasses import dataclass, field
from typing import Optional, Literal, List
from enum import Enum


class LLMProvider(str, Enum):
    """Supported LLM providers for entity extraction."""
    OPENAI = "openai"
    OLLAMA = "ollama"
    AWS_BEDROCK = "aws_bedrock"
    AZURE_OPENAI = "azure_openai"


class ExtractorType(str, Enum):
    """Entity extraction methods."""
    LLM = "llm"         # LLM-based extraction (high quality, costs money)
    NLP = "nlp"         # NLP-based extraction (fast, free, lower quality)
    HYBRID = "hybrid"   # NLP for all + LLM for important chunks
    REBEL = "rebel"     # mREBEL model (bundled, no LLM costs, 18 languages) - DEFAULT


class GraphSearchType(str, Enum):
    """Graph search strategies."""
    LOCAL = "local"     # Entity-based search with graph traversal
    GLOBAL = "global"   # Community-based search for broad queries
    HYBRID = "hybrid"   # DRIFT-style combined search (default)


@dataclass
class GraphRAGConfig:
    """
    Configuration for VectrixDB's native GraphRAG implementation.

    GraphRAG is opt-in by default. Enable it by setting `enabled=True`.

    Example:
        >>> config = GraphRAGConfig(enabled=True)
        >>> db = V("my_kb", graphrag_config=config)
        >>> db.add(texts=documents)
        >>> results = db.search("query", mode="graph")
    """

    # ===========================================
    # Enable/Disable (OPT-IN by default)
    # ===========================================
    enabled: bool = False
    """Whether GraphRAG is enabled. Must be explicitly set to True."""

    # ===========================================
    # Document Chunking
    # ===========================================
    chunk_size: int = 1200
    """Target number of tokens per chunk."""

    chunk_overlap: int = 100
    """Number of overlapping tokens between chunks."""

    chunk_by_sentence: bool = True
    """Whether to preserve sentence boundaries when chunking."""

    # ===========================================
    # Entity Extraction
    # ===========================================
    extractor: ExtractorType = ExtractorType.REBEL
    """
    Extraction method:
    - 'rebel': mREBEL model - bundled, no LLM costs, 18 languages (default, recommended)
    - 'llm': LLM-based (high quality, costs money)
    - 'nlp': NLP-based with spaCy (fast, free, lower quality)
    - 'hybrid': NLP for all + LLM for important chunks
    """

    nlp_model: str = "en_core_web_sm"
    """spaCy model for NLP extraction. Options: en_core_web_sm, en_core_web_md, en_core_web_lg"""

    # ===========================================
    # LLM Provider Configuration
    # ===========================================
    llm_provider: LLMProvider = LLMProvider.OPENAI
    """
    LLM provider for entity extraction:
    - 'openai': OpenAI API (default)
    - 'ollama': Local Ollama server
    - 'aws_bedrock': AWS Bedrock
    - 'azure_openai': Azure OpenAI Service
    """

    llm_model: str = "gpt-4o-mini"
    """
    Model name (provider-specific):
    - OpenAI: gpt-4o-mini, gpt-4o, gpt-4-turbo
    - Ollama: llama3.2, mistral, phi3
    - AWS Bedrock: anthropic.claude-3-haiku-20240307-v1:0
    - Azure OpenAI: deployment name
    """

    llm_api_key: Optional[str] = None
    """API key. If None, uses environment variable (OPENAI_API_KEY, etc.)"""

    llm_endpoint: Optional[str] = None
    """
    Custom endpoint URL:
    - Ollama: http://localhost:11434
    - Azure: https://your-resource.openai.azure.com
    """

    llm_temperature: float = 0.0
    """Temperature for LLM responses. Lower = more deterministic."""

    llm_max_tokens: int = 4096
    """Maximum tokens for LLM response."""

    # ===========================================
    # Graph Construction
    # ===========================================
    max_community_levels: int = 3
    """Maximum hierarchy depth for Leiden community detection."""

    min_community_size: int = 5
    """Minimum number of entities to form a community."""

    relationship_threshold: float = 0.3
    """Minimum relationship strength (0-1) to keep an edge."""

    deduplicate_entities: bool = True
    """Whether to merge similar entities by name."""

    entity_similarity_threshold: float = 0.85
    """Similarity threshold for entity deduplication (0-1)."""

    # ===========================================
    # Retrieval Configuration
    # ===========================================
    search_type: GraphSearchType = GraphSearchType.HYBRID
    """Default search strategy: local, global, or hybrid (DRIFT-style)."""

    local_search_k: int = 10
    """Number of seed entities for local search."""

    global_search_k: int = 5
    """Number of communities for global search."""

    traversal_depth: int = 2
    """Maximum hops from seed entities in graph traversal."""

    include_relationships: bool = True
    """Whether to include relationship context in results."""

    include_community_context: bool = True
    """Whether to include community summaries in results."""

    # ===========================================
    # Performance & Caching
    # ===========================================
    enable_incremental: bool = True
    """Enable LightRAG-style incremental updates (50% faster)."""

    batch_size: int = 50
    """Number of chunks to process per extraction batch."""

    use_cache: bool = True
    """Cache entity embeddings for faster retrieval."""

    cache_ttl: int = 3600
    """Cache time-to-live in seconds."""

    parallel_extraction: bool = True
    """Enable parallel entity extraction for large documents."""

    max_workers: int = 4
    """Maximum parallel workers for extraction."""

    # ===========================================
    # Entity Types
    # ===========================================
    entity_types: List[str] = field(default_factory=lambda: [
        "PERSON",
        "ORGANIZATION",
        "LOCATION",
        "CONCEPT",
        "EVENT",
        "OBJECT",
        "TECHNOLOGY",
        "PRODUCT",
    ])
    """Entity types to extract. Customize for domain-specific extraction."""

    # ===========================================
    # Relationship Types
    # ===========================================
    relationship_types: List[str] = field(default_factory=lambda: [
        "RELATED_TO",
        "WORKS_FOR",
        "LOCATED_IN",
        "PART_OF",
        "CREATED_BY",
        "USED_BY",
        "CAUSED_BY",
        "MENTIONS",
    ])
    """Relationship types to extract. Customize for domain-specific extraction."""

    def __post_init__(self):
        """Validate configuration after initialization."""
        if self.chunk_size < 100:
            raise ValueError("chunk_size must be at least 100 tokens")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        if not 0 <= self.relationship_threshold <= 1:
            raise ValueError("relationship_threshold must be between 0 and 1")
        if not 0 <= self.entity_similarity_threshold <= 1:
            raise ValueError("entity_similarity_threshold must be between 0 and 1")
        if self.traversal_depth < 1:
            raise ValueError("traversal_depth must be at least 1")
        if self.max_community_levels < 1:
            raise ValueError("max_community_levels must be at least 1")

    def with_openai(self, model: str = "gpt-4o-mini", api_key: Optional[str] = None) -> "GraphRAGConfig":
        """Configure for OpenAI."""
        self.llm_provider = LLMProvider.OPENAI
        self.llm_model = model
        self.llm_api_key = api_key
        return self

    def with_ollama(self, model: str = "llama3.2", endpoint: str = "http://localhost:11434") -> "GraphRAGConfig":
        """Configure for local Ollama."""
        self.llm_provider = LLMProvider.OLLAMA
        self.llm_model = model
        self.llm_endpoint = endpoint
        return self

    def with_azure(self, deployment: str, endpoint: str, api_key: Optional[str] = None) -> "GraphRAGConfig":
        """Configure for Azure OpenAI."""
        self.llm_provider = LLMProvider.AZURE_OPENAI
        self.llm_model = deployment
        self.llm_endpoint = endpoint
        self.llm_api_key = api_key
        return self

    def with_bedrock(self, model: str = "anthropic.claude-3-haiku-20240307-v1:0") -> "GraphRAGConfig":
        """Configure for AWS Bedrock."""
        self.llm_provider = LLMProvider.AWS_BEDROCK
        self.llm_model = model
        return self


# Convenience factory functions
def create_openai_config(model: str = "gpt-4o-mini", **kwargs) -> GraphRAGConfig:
    """Create a GraphRAG config for OpenAI."""
    return GraphRAGConfig(
        enabled=True,
        llm_provider=LLMProvider.OPENAI,
        llm_model=model,
        **kwargs
    )


def create_ollama_config(model: str = "llama3.2", endpoint: str = "http://localhost:11434", **kwargs) -> GraphRAGConfig:
    """Create a GraphRAG config for local Ollama."""
    return GraphRAGConfig(
        enabled=True,
        llm_provider=LLMProvider.OLLAMA,
        llm_model=model,
        llm_endpoint=endpoint,
        **kwargs
    )


def create_nlp_only_config(**kwargs) -> GraphRAGConfig:
    """Create a GraphRAG config that uses only NLP (no LLM costs). Consider using create_rebel_config() instead."""
    return GraphRAGConfig(
        enabled=True,
        extractor=ExtractorType.NLP,
        **kwargs
    )


def create_rebel_config(**kwargs) -> GraphRAGConfig:
    """
    Create a GraphRAG config that uses mREBEL (recommended, default).

    mREBEL is a bundled model that extracts triplets (entity-relation-entity)
    directly from text without LLM. Supports 18 languages.

    No API keys or external services needed.
    """
    return GraphRAGConfig(
        enabled=True,
        extractor=ExtractorType.REBEL,
        **kwargs
    )


def create_default_config(**kwargs) -> GraphRAGConfig:
    """Create the default GraphRAG config (uses mREBEL, no LLM needed)."""
    return create_rebel_config(**kwargs)
