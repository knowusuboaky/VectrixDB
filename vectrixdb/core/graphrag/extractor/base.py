"""
Base classes for entity extraction in GraphRAG.

Defines the core data structures (Entity, Relationship, ExtractionResult)
and the abstract base class for all extractors.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set, Any
from enum import Enum

from ..chunker import TextUnit


class EntityType(str, Enum):
    """Standard entity types for extraction."""
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    LOCATION = "LOCATION"
    CONCEPT = "CONCEPT"
    EVENT = "EVENT"
    OBJECT = "OBJECT"
    TECHNOLOGY = "TECHNOLOGY"
    PRODUCT = "PRODUCT"
    DATE = "DATE"
    QUANTITY = "QUANTITY"
    OTHER = "OTHER"


class RelationshipType(str, Enum):
    """Standard relationship types for extraction."""
    RELATED_TO = "RELATED_TO"
    WORKS_FOR = "WORKS_FOR"
    LOCATED_IN = "LOCATED_IN"
    PART_OF = "PART_OF"
    CREATED_BY = "CREATED_BY"
    USED_BY = "USED_BY"
    USES = "USES"
    CAUSED_BY = "CAUSED_BY"
    CAUSES = "CAUSES"
    MENTIONS = "MENTIONS"
    COLLABORATES_WITH = "COLLABORATES_WITH"
    DEPENDS_ON = "DEPENDS_ON"
    PRODUCES = "PRODUCES"
    BELONGS_TO = "BELONGS_TO"


@dataclass
class Entity:
    """
    An entity extracted from text.

    Represents a named entity (person, organization, concept, etc.)
    found during the extraction process.
    """
    id: str
    """Unique identifier for this entity."""

    name: str
    """The entity name as it appears in text."""

    type: str
    """Entity type (PERSON, ORGANIZATION, CONCEPT, etc.)."""

    description: str = ""
    """Brief description of the entity based on context."""

    source_units: List[str] = field(default_factory=list)
    """IDs of TextUnits where this entity was found."""

    importance: float = 0.0
    """Computed importance/centrality score (0-1)."""

    embedding: Optional[List[float]] = None
    """Optional embedding vector for the entity."""

    aliases: Set[str] = field(default_factory=set)
    """Alternative names or mentions of this entity."""

    attributes: Dict[str, Any] = field(default_factory=dict)
    """Additional attributes extracted for this entity."""

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if isinstance(other, Entity):
            return self.id == other.id
        return False

    def merge_with(self, other: "Entity") -> "Entity":
        """Merge another entity into this one."""
        # Combine source units
        all_sources = set(self.source_units) | set(other.source_units)

        # Combine descriptions
        if other.description and self.description:
            if other.description not in self.description:
                combined_desc = f"{self.description} {other.description}"
            else:
                combined_desc = self.description
        else:
            combined_desc = self.description or other.description

        # Combine aliases
        all_aliases = self.aliases | other.aliases
        all_aliases.add(other.name)

        # Take max importance
        max_importance = max(self.importance, other.importance)

        # Merge attributes
        merged_attrs = {**self.attributes, **other.attributes}

        return Entity(
            id=self.id,
            name=self.name,
            type=self.type,
            description=combined_desc,
            source_units=list(all_sources),
            importance=max_importance,
            embedding=self.embedding or other.embedding,
            aliases=all_aliases,
            attributes=merged_attrs
        )

    @classmethod
    def create(cls, name: str, entity_type: str, description: str = "",
               source_unit_id: Optional[str] = None) -> "Entity":
        """Factory method to create an entity with a generated ID."""
        entity_id = f"entity_{uuid.uuid4().hex[:12]}"
        source_units = [source_unit_id] if source_unit_id else []
        return cls(
            id=entity_id,
            name=name,
            type=entity_type,
            description=description,
            source_units=source_units
        )


@dataclass
class Relationship:
    """
    A relationship between two entities.

    Represents a directed edge in the knowledge graph,
    connecting a source entity to a target entity.
    """
    id: str
    """Unique identifier for this relationship."""

    source_id: str
    """ID of the source entity."""

    target_id: str
    """ID of the target entity."""

    type: str
    """Relationship type (RELATED_TO, WORKS_FOR, etc.)."""

    description: str = ""
    """Description of how the entities are related."""

    strength: float = 0.5
    """Relationship strength/weight (0-1). Higher = stronger connection."""

    source_units: List[str] = field(default_factory=list)
    """IDs of TextUnits where this relationship was found."""

    bidirectional: bool = False
    """Whether this relationship goes both ways."""

    attributes: Dict[str, Any] = field(default_factory=dict)
    """Additional attributes for this relationship."""

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if isinstance(other, Relationship):
            return self.id == other.id
        return False

    def merge_with(self, other: "Relationship") -> "Relationship":
        """Merge another relationship into this one."""
        # Combine source units
        all_sources = set(self.source_units) | set(other.source_units)

        # Combine descriptions
        if other.description and self.description:
            if other.description not in self.description:
                combined_desc = f"{self.description} {other.description}"
            else:
                combined_desc = self.description
        else:
            combined_desc = self.description or other.description

        # Average strength, weighted by occurrence count
        count1 = len(self.source_units)
        count2 = len(other.source_units)
        total = count1 + count2
        avg_strength = (self.strength * count1 + other.strength * count2) / total if total > 0 else self.strength

        # Merge attributes
        merged_attrs = {**self.attributes, **other.attributes}

        return Relationship(
            id=self.id,
            source_id=self.source_id,
            target_id=self.target_id,
            type=self.type,
            description=combined_desc,
            strength=min(1.0, avg_strength),  # Cap at 1.0
            source_units=list(all_sources),
            bidirectional=self.bidirectional or other.bidirectional,
            attributes=merged_attrs
        )

    @classmethod
    def create(cls, source_id: str, target_id: str, rel_type: str,
               description: str = "", strength: float = 0.5,
               source_unit_id: Optional[str] = None) -> "Relationship":
        """Factory method to create a relationship with a generated ID."""
        rel_id = f"rel_{uuid.uuid4().hex[:12]}"
        source_units = [source_unit_id] if source_unit_id else []
        return cls(
            id=rel_id,
            source_id=source_id,
            target_id=target_id,
            type=rel_type,
            description=description,
            strength=strength,
            source_units=source_units
        )


@dataclass
class ExtractionResult:
    """
    Result of entity and relationship extraction.

    Contains all entities and relationships extracted from a set of text units.
    """
    entities: List[Entity] = field(default_factory=list)
    """List of extracted entities."""

    relationships: List[Relationship] = field(default_factory=list)
    """List of extracted relationships."""

    source_units: List[str] = field(default_factory=list)
    """IDs of TextUnits that were processed."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Additional metadata about the extraction."""

    @property
    def entity_count(self) -> int:
        """Number of entities extracted."""
        return len(self.entities)

    @property
    def relationship_count(self) -> int:
        """Number of relationships extracted."""
        return len(self.relationships)

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        """Find an entity by name (case-insensitive)."""
        name_lower = name.lower()
        for entity in self.entities:
            if entity.name.lower() == name_lower:
                return entity
            if name_lower in {alias.lower() for alias in entity.aliases}:
                return entity
        return None

    def get_entity_by_id(self, entity_id: str) -> Optional[Entity]:
        """Find an entity by ID."""
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        return None

    def get_relationships_for_entity(self, entity_id: str) -> List[Relationship]:
        """Get all relationships involving an entity."""
        return [
            rel for rel in self.relationships
            if rel.source_id == entity_id or rel.target_id == entity_id
        ]

    def merge_with(self, other: "ExtractionResult") -> "ExtractionResult":
        """Merge another extraction result into this one."""
        # Build entity lookup by normalized name
        entity_lookup: Dict[str, Entity] = {}
        for entity in self.entities:
            key = entity.name.lower().strip()
            entity_lookup[key] = entity

        # Merge entities
        merged_entities = list(self.entities)
        entity_id_mapping: Dict[str, str] = {}  # old_id -> merged_id

        for other_entity in other.entities:
            key = other_entity.name.lower().strip()
            if key in entity_lookup:
                # Merge with existing entity
                existing = entity_lookup[key]
                merged = existing.merge_with(other_entity)
                # Replace in list
                for i, e in enumerate(merged_entities):
                    if e.id == existing.id:
                        merged_entities[i] = merged
                        break
                entity_id_mapping[other_entity.id] = existing.id
            else:
                # Add new entity
                merged_entities.append(other_entity)
                entity_lookup[key] = other_entity
                entity_id_mapping[other_entity.id] = other_entity.id

        # Build relationship lookup
        rel_lookup: Dict[tuple, Relationship] = {}
        for rel in self.relationships:
            key = (rel.source_id, rel.target_id, rel.type)
            rel_lookup[key] = rel

        # Merge relationships
        merged_relationships = list(self.relationships)
        for other_rel in other.relationships:
            # Map entity IDs
            source_id = entity_id_mapping.get(other_rel.source_id, other_rel.source_id)
            target_id = entity_id_mapping.get(other_rel.target_id, other_rel.target_id)

            key = (source_id, target_id, other_rel.type)
            if key in rel_lookup:
                # Merge with existing relationship
                existing = rel_lookup[key]
                merged = existing.merge_with(other_rel)
                # Replace in list
                for i, r in enumerate(merged_relationships):
                    if r.id == existing.id:
                        merged_relationships[i] = merged
                        break
            else:
                # Add new relationship with updated IDs
                new_rel = Relationship(
                    id=other_rel.id,
                    source_id=source_id,
                    target_id=target_id,
                    type=other_rel.type,
                    description=other_rel.description,
                    strength=other_rel.strength,
                    source_units=other_rel.source_units,
                    bidirectional=other_rel.bidirectional,
                    attributes=other_rel.attributes
                )
                merged_relationships.append(new_rel)
                rel_lookup[key] = new_rel

        # Merge source units
        all_source_units = list(set(self.source_units) | set(other.source_units))

        # Merge metadata
        merged_metadata = {**self.metadata, **other.metadata}

        return ExtractionResult(
            entities=merged_entities,
            relationships=merged_relationships,
            source_units=all_source_units,
            metadata=merged_metadata
        )


class BaseExtractor(ABC):
    """
    Abstract base class for entity extractors.

    All extractors (LLM, NLP, Hybrid) must implement this interface.
    """

    @abstractmethod
    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """
        Extract entities and relationships from text units.

        Args:
            text_units: List of TextUnit objects to process.

        Returns:
            ExtractionResult containing entities and relationships.
        """
        pass

    @abstractmethod
    def extract_single(self, text: str, doc_id: str = "default") -> ExtractionResult:
        """
        Extract from a single text string.

        Args:
            text: The text to extract from.
            doc_id: Optional document ID for tracking.

        Returns:
            ExtractionResult containing entities and relationships.
        """
        pass

    def extract_batch(self, text_units: List[TextUnit], batch_size: int = 10) -> ExtractionResult:
        """
        Extract from text units in batches.

        Default implementation processes all at once.
        Subclasses can override for batch processing.

        Args:
            text_units: List of TextUnit objects.
            batch_size: Number of units per batch.

        Returns:
            Merged ExtractionResult from all batches.
        """
        return self.extract(text_units)
