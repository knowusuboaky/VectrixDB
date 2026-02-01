"""
Global Search for VectrixDB GraphRAG.

Community-based search for broad, open-ended queries.
Uses community summaries to provide high-level context and themes.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, Set
import numpy as np

from ..graph.knowledge_graph import KnowledgeGraph
from ..graph.community import Community, CommunityHierarchy
from ..extractor.base import Entity, Relationship
from ..config import GraphRAGConfig


@dataclass
class GlobalSearchResult:
    """Result from global community-based search."""
    communities: List[Tuple[Community, float]]  # (community, score)
    context: str = ""
    entities: List[Entity] = field(default_factory=list)

    @property
    def top_communities(self) -> List[Community]:
        """Get communities sorted by score."""
        return [c for c, _ in self.communities]

    @property
    def community_summaries(self) -> List[str]:
        """Get summaries of top communities."""
        return [c.summary for c, _ in self.communities if c.summary]

    def to_context_string(self, max_communities: int = 5, max_entities: int = 10) -> str:
        """Convert to a context string for LLM."""
        lines = ["## Relevant Community Insights\n"]

        for community, score in self.communities[:max_communities]:
            lines.append(f"### Community (Score: {score:.2f})")
            if community.summary:
                lines.append(community.summary)
            lines.append(f"*{len(community.entity_ids)} entities in this community*\n")

        if self.entities:
            lines.append("\n## Key Entities\n")
            for entity in self.entities[:max_entities]:
                lines.append(f"- **{entity.name}** ({entity.type}): {entity.description}")

        return "\n".join(lines)


class GlobalSearcher:
    """
    Community-based global search for broad queries.

    Search Strategy:
    1. Score communities by relevance to query (summary similarity)
    2. Retrieve top-k communities from appropriate hierarchy levels
    3. Optionally expand to include key entities from selected communities
    4. Aggregate community summaries into context

    Best for:
    - Open-ended questions ("What are the main themes?")
    - Summary requests ("Summarize the key topics")
    - Broad exploration ("What topics are covered?")
    - High-level overviews ("What is this document about?")
    """

    def __init__(
        self,
        graph: KnowledgeGraph,
        hierarchy: CommunityHierarchy,
        config: Optional[GraphRAGConfig] = None,
        community_embeddings: Optional[Dict[str, np.ndarray]] = None
    ):
        """
        Initialize global searcher.

        Args:
            graph: The knowledge graph.
            hierarchy: The community hierarchy with summaries.
            config: GraphRAG configuration.
            community_embeddings: Pre-computed community embeddings.
        """
        self.graph = graph
        self.hierarchy = hierarchy
        self.config = config or GraphRAGConfig()
        self.community_embeddings = community_embeddings or {}

    def search(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        k: int = 5,
        level: Optional[int] = None,
        include_entities: bool = True,
        max_entities_per_community: int = 5
    ) -> GlobalSearchResult:
        """
        Search for relevant communities using global search.

        Args:
            query: The search query text.
            query_vector: Query embedding vector.
            k: Number of communities to return.
            level: Specific hierarchy level (None = all levels).
            include_entities: Whether to include key entities.
            max_entities_per_community: Max entities to include per community.

        Returns:
            GlobalSearchResult with communities and context.
        """
        if self.hierarchy.total_communities == 0:
            return GlobalSearchResult(communities=[], context="")

        # Stage 1: Get candidate communities
        candidates = self._get_candidate_communities(level)

        if not candidates:
            return GlobalSearchResult(communities=[], context="")

        # Stage 2: Score communities by relevance
        scored_communities = self._score_communities(
            candidates, query, query_vector
        )

        # Stage 3: Get top k
        top_communities = sorted(scored_communities, key=lambda x: -x[1])[:k]

        # Stage 4: Extract key entities from selected communities
        entities = []
        if include_entities:
            entities = self._get_key_entities(
                top_communities, max_entities_per_community
            )

        # Build context
        context = self._build_context(top_communities, entities)

        return GlobalSearchResult(
            communities=top_communities,
            context=context,
            entities=entities
        )

    def _get_candidate_communities(
        self,
        level: Optional[int]
    ) -> List[Community]:
        """Get candidate communities for scoring."""
        if level is not None:
            return self.hierarchy.get_level(level)

        # Get communities from all levels, preferring mid-level communities
        # which balance specificity and coverage
        all_communities = []
        num_levels = self.hierarchy.num_levels

        for lvl in range(num_levels):
            communities = self.hierarchy.get_level(lvl)
            # Weight by level (mid-levels get more candidates)
            if num_levels > 2:
                mid_level = num_levels // 2
                level_weight = 1.0 - abs(lvl - mid_level) / num_levels
            else:
                level_weight = 1.0

            for c in communities:
                c._level_weight = level_weight  # Temporary attribute for scoring
                all_communities.append(c)

        return all_communities

    def _score_communities(
        self,
        communities: List[Community],
        query: str,
        query_vector: Optional[np.ndarray]
    ) -> List[Tuple[Community, float]]:
        """Score communities by relevance to query."""
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for community in communities:
            score = 0.0

            # Summary text matching
            if community.summary:
                summary_lower = community.summary.lower()
                summary_words = set(summary_lower.split())

                # Word overlap
                overlap = len(query_words & summary_words)
                if overlap > 0:
                    score += (overlap / max(len(query_words), 1)) * 0.4

                # Exact phrase matching
                for word in query_words:
                    if word in summary_lower:
                        score += 0.1

            # Community importance (based on size and level)
            score += community.importance * 0.2

            # Size factor (larger communities may be more relevant for broad queries)
            size_factor = min(1.0, community.size / 20)
            score += size_factor * 0.1

            # Level weight (if computed)
            level_weight = getattr(community, '_level_weight', 1.0)
            score *= level_weight

            # Embedding similarity
            if query_vector is not None and community.id in self.community_embeddings:
                sim = self._cosine_similarity(
                    query_vector, self.community_embeddings[community.id]
                )
                score += sim * 0.3

            scored.append((community, score))

        return scored

    def _get_key_entities(
        self,
        communities: List[Tuple[Community, float]],
        max_per_community: int
    ) -> List[Entity]:
        """Get key entities from selected communities."""
        entities = []
        seen_ids = set()

        for community, _ in communities:
            # Get entities sorted by importance
            community_entities = []
            for entity_id in community.entity_ids:
                entity = self.graph.get_entity(entity_id)
                if entity and entity.id not in seen_ids:
                    community_entities.append(entity)

            # Sort by importance and take top k
            community_entities.sort(key=lambda e: -e.importance)
            for entity in community_entities[:max_per_community]:
                entities.append(entity)
                seen_ids.add(entity.id)

        return entities

    def _build_context(
        self,
        communities: List[Tuple[Community, float]],
        entities: List[Entity]
    ) -> str:
        """Build a context string from search results."""
        lines = []

        # Community summaries
        for community, score in communities:
            if community.summary:
                lines.append(f"[Community - {len(community.entity_ids)} entities]: {community.summary}")

        # Key entities
        if entities:
            lines.append("")
            lines.append("Key entities:")
            for entity in entities[:15]:
                lines.append(f"- {entity.name} ({entity.type}): {entity.description}")

        return "\n".join(lines)

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def set_community_embeddings(self, embeddings: Dict[str, np.ndarray]) -> None:
        """Set community embeddings for similarity search."""
        self.community_embeddings = embeddings

    def compute_community_embeddings(self, embed_fn) -> Dict[str, np.ndarray]:
        """
        Compute embeddings for all community summaries.

        Args:
            embed_fn: Function that takes text and returns embedding.

        Returns:
            Dict mapping community IDs to embeddings.
        """
        embeddings = {}
        for community in self.hierarchy.get_all_communities():
            if community.summary:
                embeddings[community.id] = embed_fn(community.summary)

        self.community_embeddings = embeddings
        return embeddings

    def search_by_level(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        k_per_level: int = 2
    ) -> Dict[int, GlobalSearchResult]:
        """
        Search each hierarchy level separately.

        Useful for getting both fine-grained and high-level results.

        Args:
            query: The search query text.
            query_vector: Query embedding vector.
            k_per_level: Number of communities per level.

        Returns:
            Dict mapping level to GlobalSearchResult.
        """
        results = {}
        for level in range(self.hierarchy.num_levels):
            results[level] = self.search(
                query=query,
                query_vector=query_vector,
                k=k_per_level,
                level=level,
                include_entities=True
            )
        return results
