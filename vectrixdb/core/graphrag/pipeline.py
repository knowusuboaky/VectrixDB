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

import logging
import os
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable, Union, Tuple, FrozenSet
from dataclasses import dataclass, field, replace
import numpy as np

from .config import GraphRAGConfig, ExtractorType, GraphSearchType
from .chunker import DocumentChunker, TextUnit
from .extractor import HybridExtractor, REBELExtractor
from .extractor.nlp_extractor import NLPExtractor
from .extractor.llm_extractor import LLMExtractor
from .extractor.base import Entity, Relationship, ExtractionResult
from .graph.knowledge_graph import KnowledgeGraph
from .graph.community import CommunityHierarchy, detect_communities
from .graph.incremental import only as _only_communities, update_hierarchy
from . import queries as _queries
from .graph.storage import GraphStorage
from .summarizer import CommunitySummarizer
from .extractor.base import BaseExtractor
from ...exceptions import GraphUnavailable
from .retriever import LocalSearcher, GlobalSearcher, HybridSearcher, GraphSearchResult


__all__ = [
    "GraphRAGStats",
    "GraphRAGPipeline",
    "create_pipeline",
]


# ============================================================================
# SETTINGS: the logger
# ============================================================================
#
# One logger for the pipeline's lines.

logger = logging.getLogger(__name__)


# ============================================================================
# THE STATISTICS
# ============================================================================
#
# INPUT   a run
# OUTPUT  what it did, counted
#
# Chunks, entities, relationships, communities, and the time each stage took.


@dataclass
class GraphRAGStats:
    """Statistics from GraphRAG processing."""

    documents_processed: int = 0
    chunks_created: int = 0
    entities_extracted: int = 0
    relationships_extracted: int = 0
    relationships_dropped: int = 0
    """Edges discarded because an endpoint was not in the graph."""

    relationships_self_loop: int = 0
    """Edges whose endpoints merged into one entity, so they joined it to itself."""

    communities_detected: int = 0
    hierarchy_reused: bool = False
    communities_recomputed: int = 0
    communities_kept: int = 0
    """Community detection and summarisation were skipped: nothing changed."""

    processing_time_ms: float = 0.0


# ============================================================================
# THE PIPELINE
# ============================================================================
#
# INPUT   documents, a config, a path and an embedder
# OUTPUT  chunking, extraction, graph construction, community detection,
#         summarisation and retrieval, in order; a factory
#
# The integration point: a collection with a graph calls this.


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

        # Neither LLMExtractor nor HybridExtractor accepts entity_types or
        # relationship_types; both read them from the config they are given.
        # Passing them anyway raised TypeError, so those two extractors could
        # not be constructed through the pipeline at all.
        extractor: BaseExtractor
        if extractor_type == ExtractorType.REBEL:
            # mREBEL: bundled model, no LLM costs, 18 languages (default)
            extractor = REBELExtractor(config=self.config)
        elif extractor_type == ExtractorType.NLP:
            extractor = NLPExtractor(
                model=self.config.nlp_model,
                config=self.config,
            )
        elif extractor_type == ExtractorType.LLM:
            extractor = LLMExtractor(config=self.config)
        else:  # HYBRID
            extractor = HybridExtractor(config=self.config)
        self.extractor = extractor

    def _init_graph(self) -> None:
        """Initialize knowledge graph."""
        self.graph = KnowledgeGraph(
            similarity_threshold=self.config.entity_similarity_threshold,
            schemas=getattr(self.config, "entity_schemas", None),
        )
        self.hierarchy: Optional[CommunityHierarchy] = None
        self._generation = 0

    def _init_storage(self) -> None:
        """Initialize persistent storage if path provided."""
        self.storage: Optional[GraphStorage] = None
        if self.path:
            # Use the path directly - caller is responsible for creating the graphrag subdirectory
            os.makedirs(self.path, exist_ok=True)
            self.storage = GraphStorage(str(self.path / "graph.db"))
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
            except Exception as exc:
                # Starting fresh is the right recovery, but doing it silently
                # means a corrupt or unreadable store looks exactly like an empty
                # one, and the user is told nothing while their graph disappears.
                logger.warning(
                    "Could not load the existing knowledge graph, starting fresh: %s", exc
                )

    def _structure_signature(self) -> Tuple[FrozenSet[str], FrozenSet[str]]:
        """What community detection depends on: which nodes and edges exist.

        Edge weights can drift as a relationship is seen again, and that is
        deliberately not part of this. Re-clustering on every strength update
        would defeat the point, and the communities a repeated sighting would
        produce are the ones already there.
        """
        return frozenset(self.graph.nodes), frozenset(self.graph.edges)

    def _rebuild_searchers(self) -> None:
        """Rebuild searchers after graph changes.

        This runs for an empty graph too. It used to return early on one, but
        add_documents() marks the pipeline built regardless, so a corpus that
        extracted no entities was "built" with no searchers and every query
        raised "Hybrid searcher not initialized". Each searcher already answers
        an empty graph with an empty result, which is the right answer.
        """
        self.local_searcher = LocalSearcher(
            graph=self.graph,
            config=self.config,
        )

        # Build these against whatever hierarchy exists, including an empty one.
        # Gating on truthiness meant a graph with no communities got no hybrid
        # searcher, and since GraphSearchType.HYBRID is the default, search then
        # raised "Hybrid searcher not initialized". A small corpus is a normal
        # state, not a broken one.
        hierarchy = self.hierarchy if self.hierarchy is not None else CommunityHierarchy()
        self.hierarchy = hierarchy

        self.global_searcher = GlobalSearcher(
            graph=self.graph,
            hierarchy=hierarchy,
            config=self.config,
        )

        self.hybrid_searcher = HybridSearcher(
            graph=self.graph,
            hierarchy=hierarchy,
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

        before_structure = self._structure_signature()

        # add_entity returns the surviving id, which differs from entity.id
        # whenever resolution merged this entity into an existing one. That
        # return value is the only place the canonical id is knowable, so it has
        # to be captured: relationships arrive carrying the extractor's
        # pre-merge ids, and an id that lost a merge is no longer in the graph.
        #
        # Discarding it meant every relationship touching a merged entity was
        # dropped by add_relationship while reporting success. Entity resolution
        # made that worse, because it merges far more aggressively than the exact
        # matching it replaced: a two-form name with one edge kept 1 of 2 edges.
        canonical_id: Dict[str, str] = {}
        for entity in all_entities:
            surviving = self.graph.add_entity(
                entity, merge_if_exists=self.config.deduplicate_entities
            )
            canonical_id[entity.id] = surviving

        for rel in all_relationships:
            if rel.strength < self.config.relationship_threshold:
                continue

            source = canonical_id.get(rel.source_id, rel.source_id)
            target = canonical_id.get(rel.target_id, rel.target_id)
            if source != rel.source_id or target != rel.target_id:
                rel = replace(rel, source_id=source, target_id=target)

            if self.graph.add_relationship(rel) is None:
                # Either an endpoint is genuinely unknown, or the two endpoints
                # merged into one entity so the edge became a self-loop. Both are
                # worth counting rather than losing silently.
                if source == target:
                    stats.relationships_self_loop += 1
                else:
                    stats.relationships_dropped += 1

        if on_progress:
            on_progress(1, 1, "building_graph")

        # Stages 4 and 5 run only when the graph's structure changed.
        # Detection and summarisation are the expensive tail of every add():
        # under an LLM extractor, summarising is one model call per community,
        # and it was paid again on every message even when the message added
        # no entity and no relationship. If the node and edge sets are the
        # same as before this batch, the previous hierarchy still describes
        # the graph and is kept as is.
        reuse = self.hierarchy is not None and self._structure_signature() == before_structure
        stats.hierarchy_reused = reuse

        if reuse:
            assert self.hierarchy is not None
            stats.communities_detected = self.hierarchy.total_communities
            if on_progress:
                on_progress(1, 1, "detecting_communities")
                on_progress(1, 1, "summarizing")
        else:
            # Stage 4: Detect communities, only where the graph changed when
            # there is a hierarchy to keep. A batch that adds a node to one
            # component cannot move community boundaries in another, so the
            # other components' communities and summaries are kept as they
            # are and only the touched components are re-detected.
            if on_progress:
                on_progress(0, 1, "detecting_communities")
            incremental = (
                getattr(self.config, "incremental_communities", True)
                and self.hierarchy is not None
                and bool(before_structure[0])
            )
            if incremental:
                assert self.hierarchy is not None
                self._generation += 1
                update = update_hierarchy(
                    self.graph,
                    self.hierarchy,
                    before_structure[0],
                    before_structure[1],
                    max_levels=self.config.max_community_levels,
                    min_community_size=self.config.min_community_size,
                    generation=self._generation,
                )
                self.hierarchy = update.hierarchy
                stats.communities_recomputed = len(update.added)
                stats.communities_kept = self.hierarchy.total_communities - len(update.added)
                to_summarize = _only_communities(self.hierarchy, update.new_ids)
            else:
                self.hierarchy = detect_communities(
                    self.graph,
                    max_levels=self.config.max_community_levels,
                    min_community_size=self.config.min_community_size,
                )
                stats.communities_recomputed = self.hierarchy.total_communities
                to_summarize = self.hierarchy
            stats.communities_detected = self.hierarchy.total_communities
            if on_progress:
                on_progress(1, 1, "detecting_communities")

            # Stage 5: Summarize the communities that are new
            if on_progress:
                on_progress(0, 1, "summarizing")

            # Use LLM for summarization only if using LLM or HYBRID extractor
            use_llm_for_summary = self.config.extractor in (
                ExtractorType.LLM,
                ExtractorType.HYBRID,
            )
            summarizer = CommunitySummarizer(
                config=self.config,
                use_llm=use_llm_for_summary,
            )
            summarizer.summarize_hierarchy(
                self.graph,
                to_summarize,
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
        # Searching an empty graph finds nothing, the same as an empty vector
        # collection. That covers both a pipeline nothing was added to and one
        # whose documents produced no entities, without treating either as an
        # error.
        if self.graph.is_empty():
            from .retriever.hybrid_search import QueryType

            search_type = search_type or self.config.search_type
            if isinstance(search_type, str):
                search_type = GraphSearchType(search_type)
            return GraphSearchResult(
                query_type=QueryType.MIXED,
                search_strategy=search_type.value,
            )

        if not self._is_built:
            raise GraphUnavailable("Graph not built. Call add_documents() first.")

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
                raise GraphUnavailable("Local searcher not initialized")
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
                raise GraphUnavailable("Global searcher not initialized")
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
                raise GraphUnavailable("Hybrid searcher not initialized")
            return self.hybrid_searcher.search(
                query=query,
                query_vector=query_vector,
                k=k,
            )

    def shortest_path(self, a: str, b: str, max_depth: int = 4) -> Dict[str, Any]:
        """Shortest chain of live relationships between two entities, by name.

        Named ``shortest_path`` because ``path`` is the pipeline's storage
        directory; ``Vectrix.graph_path()`` is the everyday spelling.
        """
        return _queries.path(self.graph, a, b, max_depth=max_depth)

    def explain(self, a: str, b: Optional[str] = None, max_depth: int = 4) -> Dict[str, Any]:
        """What the graph knows about an entity, or about how two relate."""
        return _queries.explain(self.graph, a, b, hierarchy=self.hierarchy, max_depth=max_depth)

    def supersede(
        self, old_relationship_id: str, new_relationship_id: str, when: Optional[str] = None
    ) -> bool:
        """Mark one relationship as replaced by another and persist it."""
        ok = self.graph.supersede_relationship(old_relationship_id, new_relationship_id, when)
        if ok and self.storage:
            self.storage.save_graph(self.graph)
        return ok

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
        self.graph = KnowledgeGraph(similarity_threshold=self.config.entity_similarity_threshold)
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
            # `is not None`, not truthiness: a hierarchy that has been emptied
            # still has to be written, or its stale community rows survive.
            if self.hierarchy is not None:
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
