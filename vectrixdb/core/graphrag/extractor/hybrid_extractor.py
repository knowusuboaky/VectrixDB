"""
Hybrid Extractor combining spaCy NER + mREBEL relations.

This is the recommended extractor for GraphRAG as it combines:
- spaCy: Fast multilingual NER (xx_ent_wiki_sm) + sentence segmentation (xx_sent_ud_sm)
- mREBEL: Accurate relation extraction (18 languages, INT8 quantized)

Total size: ~740 MB (spaCy ~22MB + mREBEL INT8 ~718MB)

Example:
    >>> extractor = HybridExtractor()
    >>> result = extractor.extract_single("Albert Einstein was born in Germany.")
    >>> # Entities from spaCy: Albert Einstein (PERSON), Germany (GPE)
    >>> # Relations from mREBEL: Albert Einstein --country of birth--> Germany
"""

from typing import Optional, List, Dict, Set
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


class HybridExtractor(BaseExtractor):
    """
    Hybrid entity and relationship extractor.

    Combines spaCy for fast NER with mREBEL for accurate relation extraction.
    Best of both worlds: speed + accuracy.

    Models used:
    - spaCy xx_ent_wiki_sm: Multilingual NER (~12MB, 100+ languages)
    - spaCy xx_sent_ud_sm: Sentence segmentation (~10MB)
    - mREBEL base INT8: Relation extraction (~718MB, 18 languages)
    """

    # Map spaCy entity types to our entity types
    SPACY_TO_ENTITY_TYPE = {
        "PERSON": EntityType.PERSON,
        "PER": EntityType.PERSON,
        "ORG": EntityType.ORGANIZATION,
        "GPE": EntityType.LOCATION,
        "LOC": EntityType.LOCATION,
        "FAC": EntityType.LOCATION,
        "PRODUCT": EntityType.PRODUCT,
        "EVENT": EntityType.EVENT,
        "WORK_OF_ART": EntityType.OBJECT,
        "LAW": EntityType.CONCEPT,
        "LANGUAGE": EntityType.CONCEPT,
        "DATE": EntityType.DATE,
        "TIME": EntityType.DATE,
        "MISC": EntityType.OTHER,
    }

    def __init__(
        self,
        config: Optional[GraphRAGConfig] = None,
        spacy_ner_model: str = "xx_ent_wiki_sm",
        spacy_sent_model: str = "xx_sent_ud_sm",
        use_rebel: bool = True,
    ):
        """
        Initialize hybrid extractor.

        Args:
            config: Optional GraphRAGConfig
            spacy_ner_model: spaCy model for NER (default: xx_ent_wiki_sm for multilingual)
            spacy_sent_model: spaCy model for sentence segmentation
            use_rebel: Whether to use mREBEL for relation extraction
        """
        self.config = config or GraphRAGConfig()
        self.spacy_ner_model = spacy_ner_model
        self.spacy_sent_model = spacy_sent_model
        self.use_rebel = use_rebel

        self._nlp_ner = None
        self._nlp_sent = None
        self._rebel = None

    def _ensure_spacy_loaded(self):
        """Lazy load spaCy models."""
        if self._nlp_ner is not None:
            return

        try:
            import spacy

            # Load NER model
            try:
                self._nlp_ner = spacy.load(self.spacy_ner_model)
            except OSError:
                print(f"Downloading spaCy model: {self.spacy_ner_model}")
                spacy.cli.download(self.spacy_ner_model)
                self._nlp_ner = spacy.load(self.spacy_ner_model)

            # Load sentence segmentation model (optional)
            try:
                self._nlp_sent = spacy.load(self.spacy_sent_model)
            except OSError:
                print(f"Downloading spaCy model: {self.spacy_sent_model}")
                try:
                    spacy.cli.download(self.spacy_sent_model)
                    self._nlp_sent = spacy.load(self.spacy_sent_model)
                except Exception:
                    # Sentence model is optional
                    self._nlp_sent = None

        except ImportError:
            print("spaCy not installed. Install with: pip install spacy")
            raise

    def _ensure_rebel_loaded(self):
        """Lazy load mREBEL model."""
        if not self.use_rebel or self._rebel is not None:
            return

        from ....models.embedded import REBELExtractor, is_models_installed

        if not is_models_installed("rebel"):
            print("mREBEL model not installed. Using spaCy-only extraction.")
            print("To install: vectrixdb download-models --type rebel")
            self.use_rebel = False
            return

        self._rebel = REBELExtractor()

    def _extract_entities_spacy(self, text: str, text_unit_id: str) -> List[Entity]:
        """Extract entities using spaCy NER."""
        self._ensure_spacy_loaded()

        doc = self._nlp_ner(text)
        entities = []
        seen_names: Set[str] = set()

        for ent in doc.ents:
            name = ent.text.strip()
            if not name or len(name) < 2:
                continue
            if name.lower() in seen_names:
                continue

            seen_names.add(name.lower())
            entity_type = self.SPACY_TO_ENTITY_TYPE.get(ent.label_, EntityType.OTHER)

            entity = Entity.create(
                name=name,
                entity_type=entity_type.value,
                description=f"{entity_type.value}: {name}",
                source_unit_id=text_unit_id,
            )
            entity.attributes["extractor"] = "spacy"
            entity.attributes["spacy_label"] = ent.label_
            entities.append(entity)

        return entities

    def _extract_relations_rebel(self, text: str, text_unit_id: str, entities: List[Entity]) -> List[Relationship]:
        """Extract relations using mREBEL."""
        if not self.use_rebel:
            return []

        self._ensure_rebel_loaded()
        if self._rebel is None:
            return []

        # Extract triplets
        triplets = self._rebel.extract(text)
        relationships = []

        # Build entity lookup by name (lowercase)
        entity_lookup: Dict[str, Entity] = {e.name.lower(): e for e in entities}

        for triplet in triplets:
            # Try to find matching entities
            head_entity = entity_lookup.get(triplet.head.lower())
            tail_entity = entity_lookup.get(triplet.tail.lower())

            # If entities not found by spaCy, create them from REBEL output
            if head_entity is None:
                head_entity = Entity.create(
                    name=triplet.head,
                    entity_type=self._infer_entity_type(triplet.head_type).value,
                    description=f"Entity: {triplet.head}",
                    source_unit_id=text_unit_id,
                )
                head_entity.attributes["extractor"] = "rebel"
                entities.append(head_entity)
                entity_lookup[triplet.head.lower()] = head_entity

            if tail_entity is None:
                tail_entity = Entity.create(
                    name=triplet.tail,
                    entity_type=self._infer_entity_type(triplet.tail_type).value,
                    description=f"Entity: {triplet.tail}",
                    source_unit_id=text_unit_id,
                )
                tail_entity.attributes["extractor"] = "rebel"
                entities.append(tail_entity)
                entity_lookup[triplet.tail.lower()] = tail_entity

            # Create relationship
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
            relationships.append(relationship)

        return relationships

    def _infer_entity_type(self, rebel_type: str) -> EntityType:
        """Map REBEL entity types to EntityType."""
        type_lower = rebel_type.lower()
        type_mapping = {
            "person": EntityType.PERSON,
            "per": EntityType.PERSON,
            "org": EntityType.ORGANIZATION,
            "organization": EntityType.ORGANIZATION,
            "loc": EntityType.LOCATION,
            "location": EntityType.LOCATION,
            "gpe": EntityType.LOCATION,
            "country": EntityType.LOCATION,
            "city": EntityType.LOCATION,
            "event": EntityType.EVENT,
            "product": EntityType.PRODUCT,
        }
        return type_mapping.get(type_lower, EntityType.CONCEPT)

    def _normalize_relation(self, relation: str) -> RelationshipType:
        """Normalize REBEL relation to RelationshipType."""
        relation_lower = relation.lower().replace(" ", "_")
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
            "created_by": RelationshipType.CREATED_BY,
            "founded_by": RelationshipType.CREATED_BY,
            "uses": RelationshipType.USES,
        }
        return relation_mapping.get(relation_lower, RelationshipType.RELATED_TO)

    def extract_single(self, text: str, doc_id: str = "default") -> ExtractionResult:
        """
        Extract entities and relationships from a single text.

        Uses spaCy for NER and mREBEL for relation extraction.
        """
        text_unit_id = f"{doc_id}_0"

        # Step 1: Extract entities with spaCy
        entities = self._extract_entities_spacy(text, text_unit_id)

        # Step 2: Extract relations with mREBEL (may add more entities)
        relationships = self._extract_relations_rebel(text, text_unit_id, entities)

        # Deduplicate entities
        entity_dict: Dict[str, Entity] = {}
        for entity in entities:
            key = entity.name.lower()
            if key not in entity_dict:
                entity_dict[key] = entity
            else:
                # Merge source units
                entity_dict[key].source_units.extend(entity.source_units)
                entity_dict[key].source_units = list(set(entity_dict[key].source_units))

        return ExtractionResult(
            entities=list(entity_dict.values()),
            relationships=relationships,
            metadata={
                "extractor": "hybrid",
                "spacy_model": self.spacy_ner_model,
                "rebel_model": "mrebel-base-int8" if self.use_rebel else None,
                "entities_from_spacy": sum(1 for e in entities if e.attributes.get("extractor") == "spacy"),
                "entities_from_rebel": sum(1 for e in entities if e.attributes.get("extractor") == "rebel"),
                "relationships_count": len(relationships),
            }
        )

    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """Extract from multiple text units."""
        if not text_units:
            return ExtractionResult()

        all_entities: Dict[str, Entity] = {}
        all_relationships: List[Relationship] = []

        for text_unit in text_units:
            # Extract entities with spaCy
            entities = self._extract_entities_spacy(text_unit.content, text_unit.id)

            # Extract relations with mREBEL
            relationships = self._extract_relations_rebel(text_unit.content, text_unit.id, entities)

            # Merge entities
            for entity in entities:
                key = entity.name.lower()
                if key not in all_entities:
                    all_entities[key] = entity
                else:
                    all_entities[key].source_units.extend(entity.source_units)
                    all_entities[key].source_units = list(set(all_entities[key].source_units))

            all_relationships.extend(relationships)

        return ExtractionResult(
            entities=list(all_entities.values()),
            relationships=all_relationships,
            metadata={
                "extractor": "hybrid",
                "spacy_model": self.spacy_ner_model,
                "rebel_model": "mrebel-base-int8" if self.use_rebel else None,
                "text_units_processed": len(text_units),
            }
        )

    def extract_batch(self, text_units: List[TextUnit], batch_size: int = 50) -> ExtractionResult:
        """Extract in batches."""
        return self.extract(text_units)


def create_hybrid_extractor(config: Optional[GraphRAGConfig] = None) -> HybridExtractor:
    """Factory function to create a hybrid extractor."""
    return HybridExtractor(config=config)
