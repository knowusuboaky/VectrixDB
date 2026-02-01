"""
mREBEL Triplet Extractor for VectrixDB GraphRAG.

Uses the bundled mREBEL model to extract (head, relation, tail) triplets
from text without any LLM dependency.

Supports 18 languages: ar, ca, de, el, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, sv, vi, zh

Example:
    >>> from vectrixdb.core.graphrag.extractor import REBELExtractor
    >>>
    >>> extractor = REBELExtractor()
    >>> result = extractor.extract_single("Albert Einstein was born in Germany.")
    >>> # Returns entities: Albert Einstein, Germany
    >>> # Returns relationships: Albert Einstein --country of birth--> Germany
"""

from typing import Optional, List, Dict
from .base import (
    BaseExtractor,
    Entity,
    Relationship,
    ExtractionResult,
    EntityType,
    RelationshipType,
)
from ..config import GraphRAGConfig
from ..chunker import TextUnit


class REBELExtractor(BaseExtractor):
    """
    Entity and relationship extractor using mREBEL model.

    mREBEL extracts triplets (head, relation, tail) directly from text,
    which perfectly maps to GraphRAG's entity-relationship model.

    No LLM, no API keys, no external services needed.
    Works offline after one-time model download.

    Supports 18 languages including: Arabic, Catalan, German, Greek,
    English, Spanish, French, Hindi, Italian, Japanese, Korean, Dutch,
    Polish, Portuguese, Russian, Swedish, Vietnamese, Chinese.
    """

    def __init__(self, config: Optional[GraphRAGConfig] = None):
        """
        Initialize REBEL extractor.

        Args:
            config: Optional GraphRAGConfig for settings.
        """
        self.config = config or GraphRAGConfig()
        self._extractor = None

    def _ensure_model_loaded(self):
        """Lazy load the mREBEL model."""
        if self._extractor is not None:
            return

        from ....models.embedded import REBELExtractor as ModelExtractor, is_models_installed

        # Check if model is installed
        if not is_models_installed("rebel"):
            raise FileNotFoundError(
                "mREBEL model not installed. Run:\n"
                "  vectrixdb download-models --type rebel\n"
                "or:\n"
                "  python -m vectrixdb.models.downloader --type rebel"
            )

        self._extractor = ModelExtractor()

    def _triplet_to_entities_and_relationship(
        self,
        triplet,
        doc_id: str,
        text_unit_id: str,
    ) -> tuple:
        """
        Convert a mREBEL triplet to Entity and Relationship objects.

        Args:
            triplet: Triplet object from mREBEL model
            doc_id: Document ID
            text_unit_id: Text unit ID

        Returns:
            Tuple of (head_entity, tail_entity, relationship)
        """
        # Infer entity types
        head_type = self._infer_entity_type(triplet.head_type)
        tail_type = self._infer_entity_type(triplet.tail_type)

        # Create head entity using factory method
        head_entity = Entity.create(
            name=triplet.head,
            entity_type=head_type.value,
            description=f"Entity: {triplet.head}",
            source_unit_id=text_unit_id,
        )
        head_entity.attributes["extractor"] = "rebel"
        head_entity.attributes["original_type"] = triplet.head_type

        # Create tail entity using factory method
        tail_entity = Entity.create(
            name=triplet.tail,
            entity_type=tail_type.value,
            description=f"Entity: {triplet.tail}",
            source_unit_id=text_unit_id,
        )
        tail_entity.attributes["extractor"] = "rebel"
        tail_entity.attributes["original_type"] = triplet.tail_type

        # Create relationship using factory method
        # Note: We'll update source_id and target_id after entity deduplication
        rel_type = self._normalize_relation(triplet.relation)
        relationship = Relationship.create(
            source_id=head_entity.id,
            target_id=tail_entity.id,
            rel_type=rel_type.value,
            description=triplet.relation,
            strength=1.0,
            source_unit_id=text_unit_id,
        )
        relationship.attributes["extractor"] = "rebel"
        relationship.attributes["original_relation"] = triplet.relation

        return head_entity, tail_entity, relationship

    def _infer_entity_type(self, rebel_type: str) -> EntityType:
        """Map REBEL entity types to GraphRAG EntityType."""
        type_lower = rebel_type.lower()

        # Map common types
        type_mapping = {
            "person": EntityType.PERSON,
            "per": EntityType.PERSON,
            "people": EntityType.PERSON,
            "org": EntityType.ORGANIZATION,
            "organization": EntityType.ORGANIZATION,
            "company": EntityType.ORGANIZATION,
            "loc": EntityType.LOCATION,
            "location": EntityType.LOCATION,
            "place": EntityType.LOCATION,
            "gpe": EntityType.LOCATION,
            "country": EntityType.LOCATION,
            "city": EntityType.LOCATION,
            "event": EntityType.EVENT,
            "product": EntityType.PRODUCT,
            "technology": EntityType.TECHNOLOGY,
            "tech": EntityType.TECHNOLOGY,
        }

        return type_mapping.get(type_lower, EntityType.CONCEPT)

    def _normalize_relation(self, relation: str) -> RelationshipType:
        """Normalize REBEL relation to RelationshipType."""
        relation_lower = relation.lower().replace(" ", "_")

        # Map common relations
        relation_mapping = {
            "country_of_birth": RelationshipType.LOCATED_IN,
            "place_of_birth": RelationshipType.LOCATED_IN,
            "born_in": RelationshipType.LOCATED_IN,
            "located_in": RelationshipType.LOCATED_IN,
            "headquartered_in": RelationshipType.LOCATED_IN,
            "works_for": RelationshipType.WORKS_FOR,
            "employed_by": RelationshipType.WORKS_FOR,
            "member_of": RelationshipType.PART_OF,
            "part_of": RelationshipType.PART_OF,
            "subsidiary_of": RelationshipType.PART_OF,
            "created_by": RelationshipType.CREATED_BY,
            "founded_by": RelationshipType.CREATED_BY,
            "invented_by": RelationshipType.CREATED_BY,
            "developed_by": RelationshipType.CREATED_BY,
            "used_by": RelationshipType.USED_BY,
            "uses": RelationshipType.USES,
            "caused_by": RelationshipType.CAUSED_BY,
            "causes": RelationshipType.CAUSES,
        }

        return relation_mapping.get(relation_lower, RelationshipType.RELATED_TO)

    def extract_single(self, text: str, doc_id: str = "default") -> ExtractionResult:
        """
        Extract entities and relationships from a single text.

        Args:
            text: The text to extract from
            doc_id: Document identifier

        Returns:
            ExtractionResult with entities and relationships
        """
        self._ensure_model_loaded()

        # Extract triplets using mREBEL
        triplets = self._extractor.extract(text)

        # Convert triplets to entities and relationships
        entities: Dict[str, Entity] = {}
        relationships: List[Relationship] = []
        text_unit_id = f"{doc_id}_0"

        for triplet in triplets:
            head_entity, tail_entity, rel = self._triplet_to_entities_and_relationship(
                triplet, doc_id, text_unit_id
            )

            # Deduplicate entities by name
            if head_entity.name not in entities:
                entities[head_entity.name] = head_entity
            else:
                # Merge source units
                entities[head_entity.name].source_units.extend(head_entity.source_units)
                entities[head_entity.name].source_units = list(set(entities[head_entity.name].source_units))

            if tail_entity.name not in entities:
                entities[tail_entity.name] = tail_entity
            else:
                entities[tail_entity.name].source_units.extend(tail_entity.source_units)
                entities[tail_entity.name].source_units = list(set(entities[tail_entity.name].source_units))

            relationships.append(rel)

        return ExtractionResult(
            entities=list(entities.values()),
            relationships=relationships,
            metadata={
                "extractor": "rebel",
                "model": "mrebel-base-int8",
                "triplets_found": len(triplets),
            }
        )

    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """
        Extract entities and relationships from multiple text units.

        Args:
            text_units: List of TextUnit objects to process

        Returns:
            ExtractionResult with all entities and relationships
        """
        if not text_units:
            return ExtractionResult()

        self._ensure_model_loaded()

        all_entities: Dict[str, Entity] = {}
        all_relationships: List[Relationship] = []
        total_triplets = 0

        for text_unit in text_units:
            triplets = self._extractor.extract(text_unit.content)
            total_triplets += len(triplets)

            for triplet in triplets:
                head_entity, tail_entity, rel = self._triplet_to_entities_and_relationship(
                    triplet, text_unit.doc_id, text_unit.id
                )

                # Deduplicate entities by name
                if head_entity.name not in all_entities:
                    all_entities[head_entity.name] = head_entity
                else:
                    all_entities[head_entity.name].source_units.extend(head_entity.source_units)
                    all_entities[head_entity.name].source_units = list(set(all_entities[head_entity.name].source_units))

                if tail_entity.name not in all_entities:
                    all_entities[tail_entity.name] = tail_entity
                else:
                    all_entities[tail_entity.name].source_units.extend(tail_entity.source_units)
                    all_entities[tail_entity.name].source_units = list(set(all_entities[tail_entity.name].source_units))

                all_relationships.append(rel)

        return ExtractionResult(
            entities=list(all_entities.values()),
            relationships=all_relationships,
            metadata={
                "extractor": "rebel",
                "model": "mrebel-base-int8",
                "text_units_processed": len(text_units),
                "triplets_found": total_triplets,
            }
        )

    def extract_batch(self, text_units: List[TextUnit], batch_size: int = 50) -> ExtractionResult:
        """
        Extract in batches for better performance.

        Args:
            text_units: List of TextUnit objects
            batch_size: Number of units per batch

        Returns:
            Merged ExtractionResult
        """
        # mREBEL is already efficient, so we just call extract directly
        return self.extract(text_units)


def create_rebel_extractor(config: Optional[GraphRAGConfig] = None) -> REBELExtractor:
    """
    Factory function to create a REBEL extractor.

    Args:
        config: Optional GraphRAGConfig

    Returns:
        REBELExtractor instance
    """
    return REBELExtractor(config=config)
