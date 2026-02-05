"""
GraphRAG Pipeline for VectrixDB.

Orchestrates the full GraphRAG workflow:
1. Document chunking
2. Entity and relationship extraction
3. Knowledge graph construction
4. Community detection
5. Community summarization
6. Graph-based retrieval

This is the main integration point for VectrixDB's GraphRAG capabilities.
"""

import os
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable, Union
from dataclasses import dataclass, field
import numpy as np

from .config import GraphRAGConfig, ExtractorType, GraphSearchType
from .chunker import DocumentChunker, TextUnit
from .extractor import HybridExtractor, REBELExtractor
from .extractor.nlp_extractor import NLPExtractor
from .extractor.llm_extractor import LLMExtractor
from .extractor.base import Entity, Relationship, ExtractionResult
from .graph.knowledge_graph import KnowledgeGraph
from .graph.community import CommunityHierarchy, detect_communities
from .graph.storage import GraphStorage
from .summarizer import CommunitySummarizer
from .retriever import LocalSearcher, GlobalSearcher, HybridSearcher, GraphSearchResult


@dataclass
class GraphRAGStats:
    """Statistics from GraphRAG processing."""
    documents_processed: int = 0
    chunks_created: int = 0
    entities_extracted: int = 0
    relationships_extracted: int = 0
    communities_detected: int = 0
    processing_time_ms: float = 0.0


class GraphRAGPipeline:
    """
    Main GraphRAG pipeline for VectrixDB.

    Handles the complete GraphRAG workflow from document ingestion
    to graph-based search.

    Example:
        >>> config = GraphRAGConfig(enabled=True, extractor="hybrid")
        >>> pipeline = GraphRAGPipeline(config, path="./my_kb")
        >>>
        >>> # Process documents
        >>> pipeline.add_documents(documents)
        >>>
        >>> # Search using graph
        >>> results = pipeline.search("What are the main themes?")
    """

    def __init__(
        self,
        config: GraphRAGConfig,
        path: Optional[Union[str, Path]] = None,
        embed_fn: Optional[Callable[[str], np.ndarray]] = None,
    ):
        """
        Initialize GraphRAG pipeline.

        Args:
            config: GraphRAG configuration.
            path: Storage path for persistence (None for in-memory).
            embed_fn: Optional embedding function for entity/community embeddings.
        """
        self.config = config
        self.path = Path(path) if path else None
        self.embed_fn = embed_fn

        # State tracking
        self._is_built = False
        self._stats = GraphRAGStats()

        # Initialize components
        self._init_chunker()
        self._init_extractor()
        self._init_graph()
        self._init_searchers()
        self._init_storage()

    def _init_chunker(self) -> None:
        """Initialize document chunker."""
        self.chunker = DocumentChunker(
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            chunk_by_sentence=self.config.chunk_by_sentence,
        )

    def _init_extractor(self) -> None:
        """Initialize entity extractor based on config."""
        extractor_type = self.config.extractor
        if isinstance(extractor_type, str):
            extractor_type = ExtractorType(extractor_type)

        if extractor_type == ExtractorType.REBEL:
            # mREBEL: bundled model, no LLM costs, 18 languages (default)
            self.extractor = REBELExtractor(config=self.config)
        elif extractor_type == ExtractorType.NLP:
            self.extractor = NLPExtractor(
                model=self.config.nlp_model,
                config=self.config,
            )
        elif extractor_type == ExtractorType.LLM:
            self.extractor = LLMExtractor(
                config=self.config,
                entity_types=self.config.entity_types,
                relationship_types=self.config.relationship_types,
            )
        else:  # HYBRID
            self.extractor = HybridExtractor(
                config=self.config,
                entity_types=self.config.entity_types,
                relationship_types=self.config.relationship_types,
            )

    def _init_graph(self) -> None:
        """Initialize knowledge graph."""
        self.graph = KnowledgeGraph()
        self.hierarchy: Optional[CommunityHierarchy] = None

    def _init_storage(self) -> None:
        """Initialize persistent storage if path provided."""
        self.storage: Optional[GraphStorage] = None
        if self.path:
            # Use the path directly - caller is responsible for creating the graphrag subdirectory
            os.makedirs(self.path, exist_ok=True)
            self.storage = GraphStorage(self.path / "graph.db")
            # Load existing graph if available
            self._load_from_storage()

    def _init_searchers(self) -> None:
        """Initialize search engines (after graph is ready)."""
        self.local_searcher: Optional[LocalSearcher] = None
        self.global_searcher: Optional[GlobalSearcher] = None
        self.hybrid_searcher: Optional[HybridSearcher] = None

    def _load_from_storage(self) -> None:
        """Load existing graph from storage."""
        if self.storage:
            try:
                self.graph = self.storage.load_graph()
                self.hierarchy = self.storage.load_hierarchy()
                if self.graph and not self.graph.is_empty():
                    self._is_built = True
                    self._rebuild_searchers()
            except Exception:
                pass  # Start fresh if loading fails

    def _rebuild_searchers(self) -> None:
        """Rebuild searchers after graph changes."""
        if self.graph.is_empty():
            return

        self.local_searcher = LocalSearcher(
            graph=self.graph,
            config=self.config,
        )

        if self.hierarchy:
            self.global_searcher = GlobalSearcher(
                graph=self.graph,
                hierarchy=self.hierarchy,
                config=self.config,
            )

            self.hybrid_searcher = HybridSearcher(
                graph=self.graph,
                hierarchy=self.hierarchy,
                config=self.config,
            )

        # Compute embeddings if embed_fn available
        if self.embed_fn:
            self._compute_embeddings()

    def _compute_embeddings(self) -> None:
        """Compute embeddings for entities and communities."""
        if not self.embed_fn:
            return

        # Entity embeddings
        if self.local_searcher:
            self.local_searcher.compute_entity_embeddings(self.embed_fn)

        # Community embeddings
        if self.global_searcher:
            self.global_searcher.compute_community_embeddings(self.embed_fn)

    def add_documents(
        self,
        documents: List[str],
        metadata: Optional[List[Dict[str, Any]]] = None,
        doc_ids: Optional[List[str]] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> GraphRAGStats:
        """
        Process documents and build/update the knowledge graph.

        Args:
            documents: List of document texts.
            metadata: Optional metadata for each document.
            doc_ids: Optional IDs for each document.
            on_progress: Progress callback(current, total, stage).

        Returns:
            GraphRAGStats with processing statistics.
        """
        import time
        start_time = time.perf_counter()

        stats = GraphRAGStats()
        stats.documents_processed = len(documents)

        metadata = metadata or [{} for _ in documents]
        doc_ids = doc_ids or [f"doc_{i}" for i in range(len(documents))]

        # Stage 1: Chunk documents
        if on_progress:
            on_progress(0, len(documents), "chunking")

        all_chunks: List[TextUnit] = []
        for i, (doc, meta, doc_id) in enumerate(zip(documents, metadata, doc_ids)):
            chunks = self.chunker.chunk(doc, metadata=meta, doc_id=doc_id)
            all_chunks.extend(chunks)

            if on_progress:
                on_progress(i + 1, len(documents), "chunking")

        stats.chunks_created = len(all_chunks)

        # Stage 2: Extract entities and relationships
        if on_progress:
            on_progress(0, len(all_chunks), "extracting")

        all_entities: List[Entity] = []
        all_relationships: List[Relationship] = []

        # Process in batches
        batch_size = self.config.batch_size
        for batch_start in range(0, len(all_chunks), batch_size):
            batch_end = min(batch_start + batch_size, len(all_chunks))
            batch = all_chunks[batch_start:batch_end]

            # Extract from batch - use extract_single for each chunk
            for chunk in batch:
                result = self.extractor.extract_single(chunk.text, chunk.id)
                all_entities.extend(result.entities)
                all_relationships.extend(result.relationships)

            if on_progress:
                on_progress(batch_end, len(all_chunks), "extracting")

        stats.entities_extracted = len(all_entities)
        stats.relationships_extracted = len(all_relationships)

        # Stage 3: Build/update knowledge graph
        if on_progress:
            on_progress(0, 1, "building_graph")

        for entity in all_entities:
            self.graph.add_entity(entity, merge_if_exists=self.config.deduplicate_entities)

        for rel in all_relationships:
            if rel.strength >= self.config.relationship_threshold:
                self.graph.add_relationship(rel)

        if on_progress:
            on_progress(1, 1, "building_graph")

        # Stage 4: Detect communities
        if on_progress:
            on_progress(0, 1, "detecting_communities")

        self.hierarchy = detect_communities(
            self.graph,
            max_levels=self.config.max_community_levels,
            min_community_size=self.config.min_community_size,
        )

        stats.communities_detected = self.hierarchy.total_communities

        if on_progress:
            on_progress(1, 1, "detecting_communities")

        # Stage 5: Summarize communities
        if on_progress:
            on_progress(0, 1, "summarizing")

        # Use LLM for summarization only if using LLM or HYBRID extractor
        use_llm_for_summary = self.config.extractor in (ExtractorType.LLM, ExtractorType.HYBRID)
        summarizer = CommunitySummarizer(
            config=self.config,
            use_llm=use_llm_for_summary,
        )
        self.hierarchy = summarizer.summarize_hierarchy(
            self.graph,
            self.hierarchy,
            max_workers=self.config.max_workers,
        )

        if on_progress:
            on_progress(1, 1, "summarizing")

        # Stage 6: Rebuild searchers
        self._rebuild_searchers()
        self._is_built = True

        # Stage 7: Persist if storage available
        if self.storage:
            self.storage.save_graph(self.graph)
            self.storage.save_hierarchy(self.hierarchy)

        stats.processing_time_ms = (time.perf_counter() - start_time) * 1000

        # Update cumulative stats
        self._stats.documents_processed += stats.documents_processed
        self._stats.chunks_created += stats.chunks_created
        self._stats.entities_extracted = len(self.graph.get_all_entities())
        self._stats.relationships_extracted = len(self.graph.get_all_relationships())
        self._stats.communities_detected = self.hierarchy.total_communities if self.hierarchy else 0

        return stats

    def search(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        k: int = 10,
        search_type: Optional[GraphSearchType] = None,
    ) -> GraphSearchResult:
        """
        Search the knowledge graph.

        Args:
            query: Search query text.
            query_vector: Optional query embedding.
            k: Number of results.
            search_type: Override default search type (LOCAL, GLOBAL, HYBRID).

        Returns:
            GraphSearchResult with entities, communities, and context.
        """
        if not self._is_built:
            raise RuntimeError("Graph not built. Call add_documents() first.")

        # Use configured search type if not overridden
        search_type = search_type or self.config.search_type
        if isinstance(search_type, str):
            search_type = GraphSearchType(search_type)

        # Compute query embedding if embed_fn available and not provided
        if query_vector is None and self.embed_fn:
            query_vector = self.embed_fn(query)

        # Route to appropriate searcher
        if search_type == GraphSearchType.LOCAL:
            if not self.local_searcher:
                raise RuntimeError("Local searcher not initialized")
            local_result = self.local_searcher.search(
                query=query,
                query_vector=query_vector,
                k=k,
                depth=self.config.traversal_depth,
            )
            # Convert to GraphSearchResult
            from .retriever.hybrid_search import QueryType
            return GraphSearchResult(
                query_type=QueryType.SPECIFIC,
                local_result=local_result,
                entities=local_result.entities,
                relationships=local_result.relationships,
                context=local_result.context,
                search_strategy="local",
            )

        elif search_type == GraphSearchType.GLOBAL:
            if not self.global_searcher:
                raise RuntimeError("Global searcher not initialized")
            global_result = self.global_searcher.search(
                query=query,
                query_vector=query_vector,
                k=self.config.global_search_k,
            )
            # Convert to GraphSearchResult
            from .retriever.hybrid_search import QueryType
            return GraphSearchResult(
                query_type=QueryType.BROAD,
                global_result=global_result,
                entities=[(e, 1.0) for e in global_result.entities],
                communities=global_result.communities,
                context=global_result.context,
                search_strategy="global",
            )

        else:  # HYBRID
            if not self.hybrid_searcher:
                raise RuntimeError("Hybrid searcher not initialized")
            return self.hybrid_searcher.search(
                query=query,
                query_vector=query_vector,
                k=k,
            )

    def get_entity(self, name: str) -> Optional[Entity]:
        """Get an entity by name."""
        return self.graph.get_entity_by_name(name)

    def get_neighbors(self, entity_name: str, depth: int = 1) -> Dict[str, int]:
        """Get neighbors of an entity."""
        entity = self.graph.get_entity_by_name(entity_name)
        if not entity:
            return {}
        return self.graph.get_neighbors(entity.id, depth=depth)

    def get_subgraph(self, entity_names: List[str], depth: int = 1):
        """Get a subgraph around specified entities."""
        entity_ids = []
        for name in entity_names:
            entity = self.graph.get_entity_by_name(name)
            if entity:
                entity_ids.append(entity.id)
        return self.graph.get_subgraph(entity_ids, depth=depth)

    def get_stats(self) -> GraphRAGStats:
        """Get current pipeline statistics."""
        return self._stats

    def get_graph_info(self) -> Dict[str, Any]:
        """Get information about the knowledge graph."""
        return {
            "entities": len(self.graph.get_all_entities()) if self.graph else 0,
            "relationships": len(self.graph.get_all_relationships()) if self.graph else 0,
            "communities": self.hierarchy.total_communities if self.hierarchy else 0,
            "community_levels": self.hierarchy.num_levels if self.hierarchy else 0,
            "is_built": self._is_built,
        }

    def clear(self) -> None:
        """Clear all data and reset the pipeline."""
        self.graph = KnowledgeGraph()
        self.hierarchy = None
        self._is_built = False
        self._stats = GraphRAGStats()

        if self.storage:
            self.storage.clear()

        self._init_searchers()

    def save(self) -> None:
        """Save current state to storage."""
        if self.storage:
            self.storage.save_graph(self.graph)
            if self.hierarchy:
                self.storage.save_hierarchy(self.hierarchy)

    def close(self) -> None:
        """Close the pipeline and save state."""
        self.save()
        if self.storage:
            self.storage.close()


def create_pipeline(
    config: Optional[GraphRAGConfig] = None,
    path: Optional[Union[str, Path]] = None,
    embed_fn: Optional[Callable[[str], np.ndarray]] = None,
) -> GraphRAGPipeline:
    """
    Factory function to create a GraphRAG pipeline.

    Args:
        config: GraphRAG configuration (uses defaults if None).
        path: Storage path for persistence.
        embed_fn: Optional embedding function.

    Returns:
        Configured GraphRAGPipeline instance.
    """
    config = config or GraphRAGConfig(enabled=True)
    return GraphRAGPipeline(config=config, path=path, embed_fn=embed_fn)
