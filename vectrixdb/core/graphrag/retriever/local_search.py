"""
Local Search for VectrixDB GraphRAG.

Entity-based search with graph traversal for specific, fact-finding queries.
Finds relevant entities and expands via graph connections.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, Set
import numpy as np

from ..graph.knowledge_graph import KnowledgeGraph, SubGraph
from ..extractor.base import Entity, Relationship
from ..config import GraphRAGConfig


@dataclass
class LocalSearchResult:
    """Result from local entity-based search."""
    entities: List[Tuple[Entity, float]]  # (entity, score)
    relationships: List[Relationship]
    subgraph: Optional[SubGraph] = None
    context: str = ""

    @property
    def top_entities(self) -> List[Entity]:
        """Get entities sorted by score."""
        return [e for e, _ in self.entities]

    @property
    def entity_names(self) -> List[str]:
        """Get names of top entities."""
        return [e.name for e, _ in self.entities]

    def to_context_string(self, max_entities: int = 10, max_rels: int = 15) -> str:
        """Convert to a context string for LLM."""
        lines = ["## Relevant Entities\n"]

        for entity, score in self.entities[:max_entities]:
            lines.append(f"- **{entity.name}** ({entity.type}): {entity.description}")

        if self.relationships:
            lines.append("\n## Relationships\n")
            for rel in self.relationships[:max_rels]:
                lines.append(f"- {rel.description or f'{rel.source_id} --[{rel.type}]--> {rel.target_id}'}")

        return "\n".join(lines)


class LocalSearcher:
    """
    Entity-based local search with graph traversal.

    Search Strategy:
    1. Find seed entities by name/embedding similarity
    2. Expand via graph traversal (BFS)
    3. Score expanded entities by relevance
    4. Return top entities with connecting relationships

    Best for:
    - Fact-finding queries ("What did X do?")
    - Entity-specific questions ("Who is Y?")
    - Relationship queries ("How are X and Y connected?")
    """

    def __init__(
        self,
        graph: KnowledgeGraph,
        config: Optional[GraphRAGConfig] = None,
        entity_embeddings: Optional[Dict[str, np.ndarray]] = None
    ):
        """
        Initialize local searcher.

        Args:
            graph: The knowledge graph to search.
            config: GraphRAG configuration.
            entity_embeddings: Pre-computed entity embeddings.
        """
        self.graph = graph
        self.config = config or GraphRAGConfig()
        self.entity_embeddings = entity_embeddings or {}

    def search(
        self,
        query: str,
        query_vector: Optional[np.ndarray] = None,
        k: int = 10,
        depth: int = 2,
        include_relationships: bool = True
    ) -> LocalSearchResult:
        """
        Search for relevant entities using local search.

        Args:
            query: The search query text.
            query_vector: Query embedding vector.
            k: Number of entities to return.
            depth: Graph traversal depth.
            include_relationships: Whether to include relationships.

        Returns:
            LocalSearchResult with entities and relationships.
        """
        if self.graph.is_empty():
            return LocalSearchResult(entities=[], relationships=[])

        # Stage 1: Find seed entities
        seed_entities = self._find_seed_entities(query, query_vector, k=k * 2)

        if not seed_entities:
            return LocalSearchResult(entities=[], relationships=[])

        # Stage 2: Expand via graph traversal
        expanded_ids = self._expand_seeds(seed_entities, depth)

        # Stage 3: Score all entities
        scored_entities = self._score_entities(
            expanded_ids,
            query,
            query_vector,
            seed_entities
        )

        # Stage 4: Get top k
        top_entities = sorted(scored_entities, key=lambda x: -x[1])[:k]

        # Stage 5: Get connecting relationships
        relationships = []
        if include_relationships:
            entity_ids = {e.id for e, _ in top_entities}
            relationships = self._get_relationships(entity_ids)

        # Build subgraph
        subgraph = self.graph.get_subgraph(
            [e.id for e, _ in top_entities[:5]],
            depth=1
        )

        return LocalSearchResult(
            entities=top_entities,
            relationships=relationships,
            subgraph=subgraph,
            context=self._build_context(top_entities, relationships)
        )

    def _find_seed_entities(
        self,
        query: str,
        query_vector: Optional[np.ndarray],
        k: int
    ) -> List[Tuple[Entity, float]]:
        """Find initial seed entities by name/embedding similarity."""
        candidates: List[Tuple[Entity, float]] = []

        # Name-based matching
        query_lower = query.lower()
        query_words = set(query_lower.split())

        for entity in self.graph.get_all_entities():
            name_lower = entity.name.lower()

            # Exact name match
            if name_lower in query_lower or query_lower in name_lower:
                candidates.append((entity, 1.0))
                continue

            # Word overlap
            name_words = set(name_lower.split())
            overlap = len(query_words & name_words)
            if overlap > 0:
                score = overlap / max(len(query_words), len(name_words))
                candidates.append((entity, score * 0.8))
                continue

            # Alias matching
            for alias in entity.aliases:
                alias_lower = alias.lower()
                if alias_lower in query_lower:
                    candidates.append((entity, 0.9))
                    break

        # Embedding-based matching
        if query_vector is not None and self.entity_embeddings:
            for entity_id, entity_emb in self.entity_embeddings.items():
                entity = self.graph.get_entity(entity_id)
                if entity:
                    sim = self._cosine_similarity(query_vector, entity_emb)
                    # Check if already added
                    existing = next((c for c in candidates if c[0].id == entity_id), None)
                    if existing:
                        # Boost score
                        idx = candidates.index(existing)
                        candidates[idx] = (entity, min(1.0, existing[1] + sim * 0.3))
                    else:
                        candidates.append((entity, sim * 0.7))

        # Sort and deduplicate
        seen = set()
        unique_candidates = []
        for entity, score in sorted(candidates, key=lambda x: -x[1]):
            if entity.id not in seen:
                seen.add(entity.id)
                unique_candidates.append((entity, score))

        return unique_candidates[:k]

    def _expand_seeds(
        self,
        seeds: List[Tuple[Entity, float]],
        depth: int
    ) -> Set[str]:
        """Expand seed entities via graph traversal."""
        expanded = set()

        for entity, _ in seeds:
            neighbors = self.graph.get_neighbors(entity.id, depth=depth)
            expanded.update(neighbors.keys())

        return expanded

    def _score_entities(
        self,
        entity_ids: Set[str],
        query: str,
        query_vector: Optional[np.ndarray],
        seeds: List[Tuple[Entity, float]]
    ) -> List[Tuple[Entity, float]]:
        """Score expanded entities by relevance."""
        seed_ids = {e.id for e, _ in seeds}
        seed_scores = {e.id: s for e, s in seeds}
        query_lower = query.lower()

        scored = []
        for entity_id in entity_ids:
            entity = self.graph.get_entity(entity_id)
            if not entity:
                continue

            score = 0.0

            # Base score: seed score or distance decay
            if entity_id in seed_scores:
                score += seed_scores[entity_id] * 0.4
            else:
                # Distance from nearest seed
                min_dist = float('inf')
                for seed_id in seed_ids:
                    neighbors = self.graph.get_neighbors(seed_id, depth=3)
                    if entity_id in neighbors:
                        min_dist = min(min_dist, neighbors[entity_id])
                if min_dist < float('inf'):
                    score += 0.3 * (1 / (1 + min_dist))

            # Description relevance
            if entity.description:
                desc_lower = entity.description.lower()
                query_words = set(query_lower.split())
                desc_words = set(desc_lower.split())
                overlap = len(query_words & desc_words)
                score += overlap * 0.1

            # Importance score
            score += entity.importance * 0.2

            # Embedding similarity
            if query_vector is not None and entity_id in self.entity_embeddings:
                sim = self._cosine_similarity(query_vector, self.entity_embeddings[entity_id])
                score += sim * 0.3

            scored.append((entity, score))

        return scored

    def _get_relationships(self, entity_ids: Set[str]) -> List[Relationship]:
        """Get relationships connecting the given entities."""
        relationships = []
        seen = set()

        for entity_id in entity_ids:
            for rel in self.graph.get_relationships_for_entity(entity_id):
                if rel.id in seen:
                    continue
                # Only include if both ends are in our set
                if rel.source_id in entity_ids and rel.target_id in entity_ids:
                    relationships.append(rel)
                    seen.add(rel.id)

        # Sort by strength
        return sorted(relationships, key=lambda r: -r.strength)

    def _build_context(
        self,
        entities: List[Tuple[Entity, float]],
        relationships: List[Relationship]
    ) -> str:
        """Build a context string from search results."""
        lines = []

        # Entities
        for entity, score in entities[:10]:
            lines.append(f"{entity.name} ({entity.type}): {entity.description}")

        # Relationships
        if relationships:
            lines.append("")
            for rel in relationships[:10]:
                source = self.graph.get_entity(rel.source_id)
                target = self.graph.get_entity(rel.target_id)
                if source and target:
                    lines.append(f"{source.name} --[{rel.type}]--> {target.name}: {rel.description}")

        return "\n".join(lines)

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def set_entity_embeddings(self, embeddings: Dict[str, np.ndarray]) -> None:
        """Set entity embeddings for similarity search."""
        self.entity_embeddings = embeddings

    def compute_entity_embeddings(self, embed_fn) -> Dict[str, np.ndarray]:
        """
        Compute embeddings for all entities.

        Args:
            embed_fn: Function that takes text and returns embedding.

        Returns:
            Dict mapping entity IDs to embeddings.
        """
        embeddings = {}
        for entity in self.graph.get_all_entities():
            text = f"{entity.name}: {entity.description}"
            embeddings[entity.id] = embed_fn(text)

        self.entity_embeddings = embeddings
        return embeddings
