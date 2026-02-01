"""
Hybrid Search for VectrixDB GraphRAG.

DRIFT-style combined search that intelligently routes between local and global
search strategies based on query characteristics.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, Union
from enum import Enum
import numpy as np
import re

from ..graph.knowledge_graph import KnowledgeGraph, SubGraph
from ..graph.community import Community, CommunityHierarchy
from ..extractor.base import Entity, Relationship
from ..config import GraphRAGConfig, GraphSearchType

from .local_search import LocalSearcher, LocalSearchResult
from .global_search import GlobalSearcher, GlobalSearchResult


class QueryType(str, Enum):
    """Classification of query types."""
    SPECIFIC = "specific"       # Entity-focused, fact-finding
    BROAD = "broad"             # Open-ended, thematic
    RELATIONSHIP = "relationship"  # How are X and Y connected?
    MIXED = "mixed"             # Contains both specific and broad elements


@dataclass
class GraphSearchResult:
    """Combined result from hybrid graph search."""
    query_type: QueryType
    local_result: Optional[LocalSearchResult] = None
    global_result: Optional[GlobalSearchResult] = None
    entities: List[Tuple[Entity, float]] = field(default_factory=list)
    communities: List[Tuple[Community, float]] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)
    context: str = ""
    search_strategy: str = "hybrid"

    @property
    def top_entities(self) -> List[Entity]:
        """Get top entities from combined results."""
        return [e for e, _ in self.entities]

    @property
    def top_communities(self) -> List[Community]:
        """Get top communities from combined results."""
        return [c for c, _ in self.communities]

    def to_context_string(
        self,
        max_entities: int = 10,
        max_communities: int = 3,
        max_relationships: int = 10
    ) -> str:
        """Convert to a context string for LLM."""
        lines = [f"## Search Results (Strategy: {self.search_strategy})\n"]

        # Communities (high-level context)
        if self.communities:
            lines.append("### Thematic Context\n")
            for community, score in self.communities[:max_communities]:
                if community.summary:
                    lines.append(f"- {community.summary}")
            lines.append("")

        # Entities (specific facts)
        if self.entities:
            lines.append("### Relevant Entities\n")
            for entity, score in self.entities[:max_entities]:
                lines.append(f"- **{entity.name}** ({entity.type}): {entity.description}")
            lines.append("")

        # Relationships (connections)
        if self.relationships:
            lines.append("### Key Relationships\n")
            for rel in self.relationships[:max_relationships]:
                desc = rel.description or f"{rel.type}"
                lines.append(f"- {rel.source_id} --[{desc}]--> {rel.target_id}")

        return "\n".join(lines)


class HybridSearcher:
    """
    DRIFT-style hybrid search combining local and global strategies.

    Search Flow:
    1. Classify query type (specific vs broad)
    2. Route to appropriate strategy (or both)
    3. Combine and rank results
    4. Build unified context

    DRIFT Algorithm:
    - Specific queries -> Local search (entity-based)
    - Broad queries -> Global search (community-based)
    - Mixed queries -> Both, with result fusion

    Best for:
    - Any query type (auto-routes)
    - Maximum coverage and precision
    - Production deployments
    """

    # Query classification patterns
    SPECIFIC_PATTERNS = [
        r'\bwho\s+is\b',
        r'\bwhat\s+is\b',
        r'\bwhen\s+did\b',
        r'\bwhere\s+is\b',
        r'\bhow\s+did\b',
        r'\bdefine\b',
        r'\bexplain\b.*\bspecific\b',
        r'\bdetails?\s+(about|of|on)\b',
    ]

    BROAD_PATTERNS = [
        r'\bwhat\s+are\s+the\s+(main|key|major|important)\b',
        r'\bsummarize\b',
        r'\boverview\b',
        r'\bthemes?\b',
        r'\btopics?\b',
        r'\bgenerally\b',
        r'\boverall\b',
        r'\bbroad\b',
        r'\bhigh.?level\b',
    ]

    RELATIONSHIP_PATTERNS = [
        r'\bhow\s+(is|are|does|do)\s+.+\s+(related|connected|linked)\b',
        r'\brelationship\s+between\b',
        r'\bconnection\s+between\b',
        r'\bcompare\b',
        r'\bdifference\s+between\b',
    ]

    def __init__(
        self,
        graph: KnowledgeGraph,
        hierarchy: CommunityHierarchy,
        config: Optional[GraphRAGConfig] = None,
        entity_embeddings: Optional[Dict[str, np.ndarray]] = None,
        community_embeddings: Optional[Dict[str, np.ndarray]] = None
    ):
        """
        Initialize hybrid searcher.

        Args:
            graph: The knowledge graph.
            hierarchy: The community hierarchy.
            config: GraphRAG configuration.
            entity_embeddings: Pre-computed entity embeddings.
            community_embeddings: Pre-computed community embeddings.
        """
        self.graph = graph
        self.hierarchy = hierarchy
        self.config = config or GraphRAGConfig()

        # Initialize sub-searchers
        self.local_searcher = LocalSearcher(
            graph=graph,
            config=config,
            entity_embeddings=entity_embeddings
        )

        self.global_searcher = GlobalSearcher(
            graph=graph,
            hierarchy=hierarchy,
            config=config,
            community_embeddings=community_embeddings
        )

    def search(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        k: int = 10,
        force_strategy: Optional[GraphSearchType] = None,
        local_weight: float = 0.6,
        global_weight: float = 0.4
    ) -> GraphSearchResult:
        """
        Search using hybrid DRIFT strategy.

        Args:
            query: The search query text.
            query_vector: Query embedding vector.
            k: Number of results to return.
            force_strategy: Override auto-routing (LOCAL, GLOBAL, or HYBRID).
            local_weight: Weight for local results in fusion (0-1).
            global_weight: Weight for global results in fusion (0-1).

        Returns:
            GraphSearchResult with combined results.
        """
        # Determine search strategy
        if force_strategy:
            strategy = force_strategy
            query_type = self._classify_query(query)
        else:
            query_type = self._classify_query(query)
            strategy = self._route_query(query_type)

        # Execute search based on strategy
        local_result = None
        global_result = None

        if strategy == GraphSearchType.LOCAL:
            local_result = self.local_searcher.search(
                query=query,
                query_vector=query_vector,
                k=k,
                depth=self.config.traversal_depth,
                include_relationships=self.config.include_relationships
            )
            return self._build_result_from_local(query_type, local_result)

        elif strategy == GraphSearchType.GLOBAL:
            global_result = self.global_searcher.search(
                query=query,
                query_vector=query_vector,
                k=self.config.global_search_k,
                include_entities=True
            )
            return self._build_result_from_global(query_type, global_result)

        else:  # HYBRID
            # Run both searches
            local_result = self.local_searcher.search(
                query=query,
                query_vector=query_vector,
                k=k,
                depth=self.config.traversal_depth,
                include_relationships=self.config.include_relationships
            )

            global_result = self.global_searcher.search(
                query=query,
                query_vector=query_vector,
                k=self.config.global_search_k,
                include_entities=True
            )

            # Fuse results
            return self._fuse_results(
                query_type=query_type,
                local_result=local_result,
                global_result=global_result,
                local_weight=local_weight,
                global_weight=global_weight,
                k=k
            )

    def _classify_query(self, query: str) -> QueryType:
        """Classify query type based on patterns and heuristics."""
        query_lower = query.lower()

        # Check for relationship patterns first (most specific)
        for pattern in self.RELATIONSHIP_PATTERNS:
            if re.search(pattern, query_lower):
                return QueryType.RELATIONSHIP

        specific_score = 0
        broad_score = 0

        # Check specific patterns
        for pattern in self.SPECIFIC_PATTERNS:
            if re.search(pattern, query_lower):
                specific_score += 1

        # Check broad patterns
        for pattern in self.BROAD_PATTERNS:
            if re.search(pattern, query_lower):
                broad_score += 1

        # Named entity heuristic (capitalized words suggest specific query)
        words = query.split()
        capitalized = sum(1 for w in words if w[0].isupper() and len(w) > 1)
        if capitalized >= 2:
            specific_score += 1

        # Short queries tend to be specific
        if len(words) <= 5:
            specific_score += 0.5

        # Long queries with question words tend to be broad
        if len(words) > 10:
            broad_score += 0.5

        # Determine type
        if specific_score > broad_score:
            return QueryType.SPECIFIC
        elif broad_score > specific_score:
            return QueryType.BROAD
        else:
            return QueryType.MIXED

    def _route_query(self, query_type: QueryType) -> GraphSearchType:
        """Route query to appropriate search strategy."""
        if query_type == QueryType.SPECIFIC:
            return GraphSearchType.LOCAL
        elif query_type == QueryType.BROAD:
            return GraphSearchType.GLOBAL
        elif query_type == QueryType.RELATIONSHIP:
            return GraphSearchType.LOCAL  # Relationships need entity traversal
        else:  # MIXED
            return GraphSearchType.HYBRID

    def _build_result_from_local(
        self,
        query_type: QueryType,
        local_result: LocalSearchResult
    ) -> GraphSearchResult:
        """Build hybrid result from local-only search."""
        return GraphSearchResult(
            query_type=query_type,
            local_result=local_result,
            entities=local_result.entities,
            relationships=local_result.relationships,
            context=local_result.context,
            search_strategy="local"
        )

    def _build_result_from_global(
        self,
        query_type: QueryType,
        global_result: GlobalSearchResult
    ) -> GraphSearchResult:
        """Build hybrid result from global-only search."""
        # Convert entities list to tuple format
        entities = [(e, 1.0) for e in global_result.entities]

        return GraphSearchResult(
            query_type=query_type,
            global_result=global_result,
            entities=entities,
            communities=global_result.communities,
            context=global_result.context,
            search_strategy="global"
        )

    def _fuse_results(
        self,
        query_type: QueryType,
        local_result: LocalSearchResult,
        global_result: GlobalSearchResult,
        local_weight: float,
        global_weight: float,
        k: int
    ) -> GraphSearchResult:
        """Fuse local and global results using weighted combination."""
        # Normalize weights
        total_weight = local_weight + global_weight
        local_weight = local_weight / total_weight
        global_weight = global_weight / total_weight

        # Fuse entities
        entity_scores: Dict[str, Tuple[Entity, float]] = {}

        # Add local entities with weight
        for entity, score in local_result.entities:
            entity_scores[entity.id] = (entity, score * local_weight)

        # Add global entities with weight
        for entity in global_result.entities:
            if entity.id in entity_scores:
                existing = entity_scores[entity.id]
                entity_scores[entity.id] = (entity, existing[1] + global_weight)
            else:
                entity_scores[entity.id] = (entity, global_weight * 0.5)

        # Sort and get top k entities
        fused_entities = sorted(
            entity_scores.values(),
            key=lambda x: -x[1]
        )[:k]

        # Communities from global (already scored)
        communities = global_result.communities

        # Relationships from local
        relationships = local_result.relationships

        # Build combined context
        context = self._build_fused_context(
            fused_entities, communities, relationships
        )

        return GraphSearchResult(
            query_type=query_type,
            local_result=local_result,
            global_result=global_result,
            entities=fused_entities,
            communities=communities,
            relationships=relationships,
            context=context,
            search_strategy="hybrid"
        )

    def _build_fused_context(
        self,
        entities: List[Tuple[Entity, float]],
        communities: List[Tuple[Community, float]],
        relationships: List[Relationship]
    ) -> str:
        """Build context string from fused results."""
        lines = []

        # Community context (high-level)
        if communities:
            lines.append("Thematic context:")
            for community, _ in communities[:3]:
                if community.summary:
                    lines.append(f"- {community.summary}")
            lines.append("")

        # Entity details
        if entities:
            lines.append("Relevant entities:")
            for entity, score in entities[:10]:
                lines.append(f"- {entity.name} ({entity.type}): {entity.description}")
            lines.append("")

        # Relationships
        if relationships:
            lines.append("Key relationships:")
            for rel in relationships[:8]:
                source = self.graph.get_entity(rel.source_id)
                target = self.graph.get_entity(rel.target_id)
                if source and target:
                    lines.append(
                        f"- {source.name} --[{rel.type}]--> {target.name}"
                    )

        return "\n".join(lines)

    def set_embeddings(
        self,
        entity_embeddings: Optional[Dict[str, np.ndarray]] = None,
        community_embeddings: Optional[Dict[str, np.ndarray]] = None
    ) -> None:
        """Set embeddings for both searchers."""
        if entity_embeddings:
            self.local_searcher.set_entity_embeddings(entity_embeddings)
        if community_embeddings:
            self.global_searcher.set_community_embeddings(community_embeddings)

    def compute_all_embeddings(self, embed_fn) -> None:
        """
        Compute all embeddings for entities and communities.

        Args:
            embed_fn: Function that takes text and returns embedding.
        """
        self.local_searcher.compute_entity_embeddings(embed_fn)
        self.global_searcher.compute_community_embeddings(embed_fn)


def create_hybrid_searcher(
    graph: KnowledgeGraph,
    hierarchy: CommunityHierarchy,
    config: Optional[GraphRAGConfig] = None
) -> HybridSearcher:
    """
    Factory function to create a hybrid searcher.

    Args:
        graph: Knowledge graph.
        hierarchy: Community hierarchy.
        config: Optional GraphRAG config.

    Returns:
        Configured HybridSearcher instance.
    """
    return HybridSearcher(
        graph=graph,
        hierarchy=hierarchy,
        config=config
    )
