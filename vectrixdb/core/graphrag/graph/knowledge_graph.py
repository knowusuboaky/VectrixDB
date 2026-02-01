"""
Knowledge Graph for VectrixDB GraphRAG.

In-memory graph structure with entity nodes, relationship edges,
and support for incremental updates (LightRAG-style).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, Tuple, Iterator, Any
from difflib import SequenceMatcher

from ..extractor.base import Entity, Relationship, ExtractionResult


@dataclass
class SubGraph:
    """A subset of the knowledge graph."""
    entities: List[Entity]
    relationships: List[Relationship]
    center_entity_id: Optional[str] = None

    @property
    def entity_ids(self) -> Set[str]:
        return {e.id for e in self.entities}

    @property
    def size(self) -> int:
        return len(self.entities)


class KnowledgeGraph:
    """
    In-memory knowledge graph with entity deduplication and incremental updates.

    Features:
    - Entity nodes with descriptions and importance scores
    - Relationship edges with strength and type
    - Name-based entity deduplication
    - Incremental updates (LightRAG-style union merge)
    - Graph traversal (BFS/DFS)
    - Subgraph extraction

    Example:
        >>> graph = KnowledgeGraph()
        >>> graph.add_entity(Entity.create("Apple", "ORGANIZATION"))
        >>> graph.add_entity(Entity.create("Steve Jobs", "PERSON"))
        >>> graph.add_relationship(Relationship.create(
        ...     source_id="...", target_id="...", rel_type="CREATED_BY"
        ... ))
    """

    def __init__(self, similarity_threshold: float = 0.85):
        """
        Initialize the knowledge graph.

        Args:
            similarity_threshold: Threshold for entity deduplication (0-1).
        """
        self.similarity_threshold = similarity_threshold

        # Core storage
        self.nodes: Dict[str, Entity] = {}
        self.edges: Dict[str, Relationship] = {}

        # Index structures
        self._name_to_entity: Dict[str, str] = {}  # normalized_name -> entity_id
        self._entity_edges: Dict[str, Set[str]] = defaultdict(set)  # entity_id -> set of edge_ids
        self._outgoing_edges: Dict[str, Set[str]] = defaultdict(set)  # entity_id -> outgoing edge_ids
        self._incoming_edges: Dict[str, Set[str]] = defaultdict(set)  # entity_id -> incoming edge_ids

        # Statistics
        self._entity_count = 0
        self._relationship_count = 0

    @property
    def entity_count(self) -> int:
        return len(self.nodes)

    @property
    def relationship_count(self) -> int:
        return len(self.edges)

    def is_empty(self) -> bool:
        return len(self.nodes) == 0

    def _normalize_name(self, name: str) -> str:
        """Normalize entity name for matching."""
        return name.lower().strip()

    def _compute_similarity(self, name1: str, name2: str) -> float:
        """Compute similarity between two names."""
        return SequenceMatcher(None, name1.lower(), name2.lower()).ratio()

    def _find_similar_entity(self, name: str) -> Optional[str]:
        """Find an existing entity with a similar name."""
        normalized = self._normalize_name(name)

        # Exact match first
        if normalized in self._name_to_entity:
            return self._name_to_entity[normalized]

        # Fuzzy match if threshold is less than 1.0
        if self.similarity_threshold < 1.0:
            for existing_name, entity_id in self._name_to_entity.items():
                if self._compute_similarity(normalized, existing_name) >= self.similarity_threshold:
                    return entity_id

        return None

    def add_entity(self, entity: Entity, merge_if_exists: bool = True) -> str:
        """
        Add an entity to the graph.

        Args:
            entity: The entity to add.
            merge_if_exists: Whether to merge with existing similar entity.

        Returns:
            The entity ID (may be existing entity if merged).
        """
        # Check for existing similar entity
        existing_id = self._find_similar_entity(entity.name)

        if existing_id and merge_if_exists:
            # Merge with existing entity
            existing = self.nodes[existing_id]
            merged = existing.merge_with(entity)
            self.nodes[existing_id] = merged

            # Update name index with alias
            self._name_to_entity[self._normalize_name(entity.name)] = existing_id

            return existing_id
        else:
            # Add as new entity
            self.nodes[entity.id] = entity
            self._name_to_entity[self._normalize_name(entity.name)] = entity.id

            # Add aliases to index
            for alias in entity.aliases:
                self._name_to_entity[self._normalize_name(alias)] = entity.id

            self._entity_count += 1
            return entity.id

    def add_relationship(self, relationship: Relationship, merge_if_exists: bool = True) -> str:
        """
        Add a relationship to the graph.

        Args:
            relationship: The relationship to add.
            merge_if_exists: Whether to merge with existing similar relationship.

        Returns:
            The relationship ID.
        """
        # Check if source and target entities exist
        if relationship.source_id not in self.nodes or relationship.target_id not in self.nodes:
            # Try to find by name
            return relationship.id

        # Check for existing similar relationship
        existing_key = (relationship.source_id, relationship.target_id, relationship.type)

        for edge_id, edge in self.edges.items():
            if (edge.source_id, edge.target_id, edge.type) == existing_key:
                if merge_if_exists:
                    merged = edge.merge_with(relationship)
                    self.edges[edge_id] = merged
                    return edge_id
                else:
                    return edge_id

        # Add as new relationship
        self.edges[relationship.id] = relationship

        # Update indexes
        self._entity_edges[relationship.source_id].add(relationship.id)
        self._entity_edges[relationship.target_id].add(relationship.id)
        self._outgoing_edges[relationship.source_id].add(relationship.id)
        self._incoming_edges[relationship.target_id].add(relationship.id)

        self._relationship_count += 1
        return relationship.id

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Get an entity by ID."""
        return self.nodes.get(entity_id)

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        """Get an entity by name."""
        entity_id = self._name_to_entity.get(self._normalize_name(name))
        if entity_id:
            return self.nodes.get(entity_id)
        return None

    def get_relationship(self, relationship_id: str) -> Optional[Relationship]:
        """Get a relationship by ID."""
        return self.edges.get(relationship_id)

    def get_relationships_for_entity(self, entity_id: str) -> List[Relationship]:
        """Get all relationships involving an entity."""
        edge_ids = self._entity_edges.get(entity_id, set())
        return [self.edges[eid] for eid in edge_ids if eid in self.edges]

    def get_outgoing_relationships(self, entity_id: str) -> List[Relationship]:
        """Get outgoing relationships from an entity."""
        edge_ids = self._outgoing_edges.get(entity_id, set())
        return [self.edges[eid] for eid in edge_ids if eid in self.edges]

    def get_incoming_relationships(self, entity_id: str) -> List[Relationship]:
        """Get incoming relationships to an entity."""
        edge_ids = self._incoming_edges.get(entity_id, set())
        return [self.edges[eid] for eid in edge_ids if eid in self.edges]

    def get_neighbors(self, entity_id: str, depth: int = 1) -> Dict[str, int]:
        """
        Get neighboring entities within a given depth.

        Args:
            entity_id: Starting entity ID.
            depth: Maximum traversal depth.

        Returns:
            Dict mapping entity IDs to their distance from the start.
        """
        if entity_id not in self.nodes:
            return {}

        visited: Dict[str, int] = {entity_id: 0}
        frontier = [entity_id]

        for current_depth in range(depth):
            next_frontier = []
            for node_id in frontier:
                # Get all connected entities
                for edge_id in self._entity_edges.get(node_id, set()):
                    edge = self.edges.get(edge_id)
                    if not edge:
                        continue

                    # Find the other end of the edge
                    neighbor_id = edge.target_id if edge.source_id == node_id else edge.source_id

                    if neighbor_id not in visited:
                        visited[neighbor_id] = current_depth + 1
                        next_frontier.append(neighbor_id)

            frontier = next_frontier

        return visited

    def get_subgraph(
        self,
        entity_ids: List[str],
        depth: int = 1,
        include_connecting: bool = True
    ) -> SubGraph:
        """
        Extract a subgraph centered on given entities.

        Args:
            entity_ids: Center entity IDs.
            depth: Expansion depth.
            include_connecting: Include relationships connecting expanded entities.

        Returns:
            SubGraph containing the extracted nodes and edges.
        """
        # Expand from each seed entity
        all_entity_ids: Set[str] = set()
        for eid in entity_ids:
            neighbors = self.get_neighbors(eid, depth)
            all_entity_ids.update(neighbors.keys())

        # Collect entities
        entities = [self.nodes[eid] for eid in all_entity_ids if eid in self.nodes]

        # Collect relationships
        relationships = []
        if include_connecting:
            seen_edges: Set[str] = set()
            for eid in all_entity_ids:
                for edge_id in self._entity_edges.get(eid, set()):
                    if edge_id in seen_edges:
                        continue
                    edge = self.edges.get(edge_id)
                    if edge and edge.source_id in all_entity_ids and edge.target_id in all_entity_ids:
                        relationships.append(edge)
                        seen_edges.add(edge_id)

        return SubGraph(
            entities=entities,
            relationships=relationships,
            center_entity_id=entity_ids[0] if entity_ids else None
        )

    def merge_extraction(self, result: ExtractionResult) -> None:
        """
        Merge an extraction result into the graph (incremental update).

        LightRAG-style union-based merge:
        - New entities are added or merged with existing similar ones
        - New relationships are added or merged with existing ones
        - No full reconstruction needed (~50% faster)
        """
        # Map old entity IDs to new/existing IDs
        id_mapping: Dict[str, str] = {}

        # Add/merge entities
        for entity in result.entities:
            new_id = self.add_entity(entity, merge_if_exists=True)
            id_mapping[entity.id] = new_id

        # Add/merge relationships with remapped IDs
        for rel in result.relationships:
            new_source_id = id_mapping.get(rel.source_id, rel.source_id)
            new_target_id = id_mapping.get(rel.target_id, rel.target_id)

            # Create relationship with updated IDs
            updated_rel = Relationship(
                id=rel.id,
                source_id=new_source_id,
                target_id=new_target_id,
                type=rel.type,
                description=rel.description,
                strength=rel.strength,
                source_units=rel.source_units,
                bidirectional=rel.bidirectional,
                attributes=rel.attributes
            )
            self.add_relationship(updated_rel, merge_if_exists=True)

    def compute_entity_importance(self) -> None:
        """
        Compute importance scores for all entities.

        Uses a simple degree-based centrality metric.
        Higher degree = more important.
        """
        if not self.nodes:
            return

        # Count connections for each entity
        max_degree = 1
        for entity_id in self.nodes:
            degree = len(self._entity_edges.get(entity_id, set()))
            max_degree = max(max_degree, degree)

        # Normalize to 0-1
        for entity_id, entity in self.nodes.items():
            degree = len(self._entity_edges.get(entity_id, set()))
            entity.importance = degree / max_degree

    def get_top_entities(self, n: int = 10) -> List[Entity]:
        """Get the top N most important entities."""
        sorted_entities = sorted(
            self.nodes.values(),
            key=lambda e: e.importance,
            reverse=True
        )
        return sorted_entities[:n]

    def get_all_entities(self) -> List[Entity]:
        """Get all entities in the graph."""
        return list(self.nodes.values())

    def get_all_relationships(self) -> List[Relationship]:
        """Get all relationships in the graph."""
        return list(self.edges.values())

    def remove_entity(self, entity_id: str) -> bool:
        """Remove an entity and its relationships."""
        if entity_id not in self.nodes:
            return False

        entity = self.nodes[entity_id]

        # Remove from name index
        self._name_to_entity.pop(self._normalize_name(entity.name), None)
        for alias in entity.aliases:
            self._name_to_entity.pop(self._normalize_name(alias), None)

        # Remove related edges
        for edge_id in list(self._entity_edges.get(entity_id, set())):
            self.edges.pop(edge_id, None)
            self._relationship_count = max(0, self._relationship_count - 1)

        # Clean up indexes
        self._entity_edges.pop(entity_id, None)
        self._outgoing_edges.pop(entity_id, None)
        self._incoming_edges.pop(entity_id, None)

        # Remove entity
        del self.nodes[entity_id]
        self._entity_count = max(0, self._entity_count - 1)

        return True

    def clear(self) -> None:
        """Clear all data from the graph."""
        self.nodes.clear()
        self.edges.clear()
        self._name_to_entity.clear()
        self._entity_edges.clear()
        self._outgoing_edges.clear()
        self._incoming_edges.clear()
        self._entity_count = 0
        self._relationship_count = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the graph to a dictionary."""
        return {
            "entities": [
                {
                    "id": e.id,
                    "name": e.name,
                    "type": e.type,
                    "description": e.description,
                    "importance": e.importance,
                    "source_units": e.source_units,
                    "aliases": list(e.aliases),
                    "attributes": e.attributes,
                }
                for e in self.nodes.values()
            ],
            "relationships": [
                {
                    "id": r.id,
                    "source_id": r.source_id,
                    "target_id": r.target_id,
                    "type": r.type,
                    "description": r.description,
                    "strength": r.strength,
                    "source_units": r.source_units,
                    "bidirectional": r.bidirectional,
                    "attributes": r.attributes,
                }
                for r in self.edges.values()
            ],
            "metadata": {
                "entity_count": self.entity_count,
                "relationship_count": self.relationship_count,
            }
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeGraph":
        """Deserialize a graph from a dictionary."""
        graph = cls()

        # Load entities
        for e_data in data.get("entities", []):
            entity = Entity(
                id=e_data["id"],
                name=e_data["name"],
                type=e_data["type"],
                description=e_data.get("description", ""),
                importance=e_data.get("importance", 0.0),
                source_units=e_data.get("source_units", []),
                aliases=set(e_data.get("aliases", [])),
                attributes=e_data.get("attributes", {}),
            )
            graph.add_entity(entity, merge_if_exists=False)

        # Load relationships
        for r_data in data.get("relationships", []):
            relationship = Relationship(
                id=r_data["id"],
                source_id=r_data["source_id"],
                target_id=r_data["target_id"],
                type=r_data["type"],
                description=r_data.get("description", ""),
                strength=r_data.get("strength", 0.5),
                source_units=r_data.get("source_units", []),
                bidirectional=r_data.get("bidirectional", False),
                attributes=r_data.get("attributes", {}),
            )
            graph.add_relationship(relationship, merge_if_exists=False)

        return graph

    def to_ascii(self, max_entities: int = 20, max_name_len: int = 25) -> str:
        """
        Generate ASCII art visualization of the knowledge graph.

        Args:
            max_entities: Maximum number of entities to show
            max_name_len: Maximum length for entity names

        Returns:
            ASCII string representation of the graph
        """
        if self.is_empty():
            return """
╔══════════════════════════════════════════════════════╗
║           VECTRIXDB KNOWLEDGE GRAPH                  ║
╠══════════════════════════════════════════════════════╣
║  (empty - no entities or relationships yet)          ║
╚══════════════════════════════════════════════════════╝
"""

        lines = []
        lines.append("╔" + "═" * 60 + "╗")
        lines.append("║" + "VECTRIXDB KNOWLEDGE GRAPH".center(60) + "║")
        lines.append("╠" + "═" * 60 + "╣")
        lines.append(f"║  Entities: {self.entity_count:<10}  Relationships: {self.relationship_count:<10}     ║")
        lines.append("╠" + "═" * 60 + "╣")

        # Get top entities by importance
        self.compute_entity_importance()
        top_entities = self.get_top_entities(max_entities)

        # Group by type
        by_type: Dict[str, List[Entity]] = defaultdict(list)
        for entity in top_entities:
            by_type[entity.type].append(entity)

        # Display entities by type
        lines.append("║  ENTITIES:".ljust(61) + "║")
        for etype, entities in sorted(by_type.items()):
            type_icon = {
                "PERSON": "👤",
                "ORGANIZATION": "🏢",
                "LOCATION": "📍",
                "EVENT": "📅",
                "CONCEPT": "💡",
                "PRODUCT": "📦",
                "TECHNOLOGY": "⚙️",
            }.get(etype.upper(), "●")

            lines.append(f"║    [{etype}]".ljust(61) + "║")
            for e in entities[:5]:  # Max 5 per type
                name = e.name[:max_name_len] + "..." if len(e.name) > max_name_len else e.name
                importance = f"{e.importance:.2f}"
                lines.append(f"║      {type_icon} {name:<30} (imp: {importance})".ljust(61) + "║")

        lines.append("╠" + "═" * 60 + "╣")

        # Display relationships
        lines.append("║  RELATIONSHIPS:".ljust(61) + "║")
        rel_count = 0
        for rel in list(self.edges.values())[:15]:  # Show first 15
            source = self.nodes.get(rel.source_id)
            target = self.nodes.get(rel.target_id)
            if source and target:
                src_name = source.name[:12] + ".." if len(source.name) > 14 else source.name
                tgt_name = target.name[:12] + ".." if len(target.name) > 14 else target.name
                rel_type = rel.type[:15] if len(rel.type) > 15 else rel.type

                arrow = "←→" if rel.bidirectional else "──→"
                line = f"║    {src_name} {arrow}[{rel_type}]{arrow} {tgt_name}"
                lines.append(line.ljust(61) + "║")
                rel_count += 1

        if self.relationship_count > 15:
            lines.append(f"║    ... and {self.relationship_count - 15} more relationships".ljust(61) + "║")

        lines.append("╚" + "═" * 60 + "╝")

        # Add graph visualization
        lines.append("")
        lines.append("  Graph Structure:")
        lines.append("  " + "─" * 40)

        # Simple node-edge ASCII representation
        displayed = set()
        edge_lines = []
        for rel in list(self.edges.values())[:10]:
            source = self.nodes.get(rel.source_id)
            target = self.nodes.get(rel.target_id)
            if source and target:
                src_short = source.name[:15]
                tgt_short = target.name[:15]
                rel_short = rel.type[:10]

                if rel.bidirectional:
                    edge_lines.append(f"  [{src_short}] ◄──({rel_short})──► [{tgt_short}]")
                else:
                    edge_lines.append(f"  [{src_short}] ───({rel_short})───► [{tgt_short}]")

        lines.extend(edge_lines[:10])

        if self.relationship_count > 10:
            lines.append(f"  ... ({self.relationship_count - 10} more connections)")

        return "\n".join(lines)

    def print_ascii(self, max_entities: int = 20) -> None:
        """Print ASCII visualization to console."""
        print(self.to_ascii(max_entities))
