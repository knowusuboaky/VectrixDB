"""
NLP-based Entity Extractor for GraphRAG.

Uses spaCy for fast, free entity extraction without requiring an LLM.
Extracts named entities and infers relationships from co-occurrence.
"""

import re
from collections import defaultdict
from typing import List, Dict, Set, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import (
    BaseExtractor,
    Entity,
    Relationship,
    ExtractionResult,
    EntityType,
    RelationshipType,
)
from ..chunker import TextUnit
from ..config import GraphRAGConfig


class NLPExtractor(BaseExtractor):
    """
    Fast entity extraction using spaCy NER.

    No LLM required - uses traditional NLP techniques for
    named entity recognition and co-occurrence based relationships.

    ~10x faster and free compared to LLM extraction.

    Example:
        >>> extractor = NLPExtractor()
        >>> result = extractor.extract_single("Apple Inc. was founded by Steve Jobs.")
        >>> print(result.entities[0].name)  # "Apple Inc."
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
        "PERCENT": EntityType.QUANTITY,
        "MONEY": EntityType.QUANTITY,
        "QUANTITY": EntityType.QUANTITY,
        "ORDINAL": EntityType.QUANTITY,
        "CARDINAL": EntityType.QUANTITY,
        "NORP": EntityType.ORGANIZATION,  # Nationalities, religious, political groups
    }

    def __init__(
        self,
        model: str = "en_core_web_sm",
        config: Optional[GraphRAGConfig] = None,
        extract_noun_phrases: bool = True,
        min_entity_length: int = 2,
        max_entity_length: int = 100,
    ):
        """
        Initialize the NLP extractor.

        Args:
            model: spaCy model name (en_core_web_sm, en_core_web_md, en_core_web_lg).
            config: Optional GraphRAGConfig for settings.
            extract_noun_phrases: Whether to extract noun phrases as concepts.
            min_entity_length: Minimum character length for entities.
            max_entity_length: Maximum character length for entities.
        """
        self.model_name = model if not config else config.nlp_model
        self.extract_noun_phrases = extract_noun_phrases
        self.min_entity_length = min_entity_length
        self.max_entity_length = max_entity_length

        self._nlp = None
        self._load_model()

    def _load_model(self):
        """Load the spaCy model."""
        try:
            import spacy
            try:
                self._nlp = spacy.load(self.model_name)
            except OSError:
                # Model not found, try to download it
                print(f"Downloading spaCy model: {self.model_name}")
                spacy.cli.download(self.model_name)
                self._nlp = spacy.load(self.model_name)
        except ImportError:
            # spaCy not installed, use fallback
            print("spaCy not installed. Using basic regex extraction.")
            self._nlp = None

    def _normalize_entity_name(self, name: str) -> str:
        """Normalize entity name for deduplication."""
        # Remove extra whitespace
        name = " ".join(name.split())
        # Remove leading/trailing punctuation
        name = name.strip(".,;:!?\"'()[]{}")
        return name

    def _is_valid_entity(self, name: str) -> bool:
        """Check if entity name is valid."""
        if not name or len(name) < self.min_entity_length:
            return False
        if len(name) > self.max_entity_length:
            return False
        # Skip pure numbers or single characters
        if name.isdigit() or len(name) == 1:
            return False
        # Skip common stopwords
        stopwords = {"the", "a", "an", "this", "that", "it", "he", "she", "they"}
        if name.lower() in stopwords:
            return False
        return True

    def _extract_with_spacy(self, text: str, text_unit_id: str) -> Tuple[List[Entity], List[Tuple[str, str, int]]]:
        """Extract entities using spaCy."""
        doc = self._nlp(text)
        entities = []
        entity_positions: List[Tuple[str, str, int]] = []  # (name, type, position)

        # Extract named entities
        for ent in doc.ents:
            name = self._normalize_entity_name(ent.text)
            if not self._is_valid_entity(name):
                continue

            entity_type = self.SPACY_TO_ENTITY_TYPE.get(ent.label_, EntityType.OTHER)

            entity = Entity.create(
                name=name,
                entity_type=entity_type.value,
                description=f"{entity_type.value}: {name}",
                source_unit_id=text_unit_id
            )
            entities.append(entity)
            entity_positions.append((name, entity_type.value, ent.start_char))

        # Extract noun phrases as concepts (optional)
        if self.extract_noun_phrases:
            seen_names = {e.name.lower() for e in entities}
            for chunk in doc.noun_chunks:
                name = self._normalize_entity_name(chunk.text)
                if not self._is_valid_entity(name):
                    continue
                if name.lower() in seen_names:
                    continue

                # Only include substantial noun phrases
                if len(name.split()) >= 2 or (chunk.root.pos_ in {"NOUN", "PROPN"} and len(name) > 5):
                    entity = Entity.create(
                        name=name,
                        entity_type=EntityType.CONCEPT.value,
                        description=f"Concept: {name}",
                        source_unit_id=text_unit_id
                    )
                    entities.append(entity)
                    entity_positions.append((name, EntityType.CONCEPT.value, chunk.start_char))
                    seen_names.add(name.lower())

        return entities, entity_positions

    def _extract_with_regex(self, text: str, text_unit_id: str) -> Tuple[List[Entity], List[Tuple[str, str, int]]]:
        """Fallback extraction using regex when spaCy is not available."""
        entities = []
        entity_positions = []

        # Extract capitalized phrases (likely proper nouns)
        pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b'
        for match in re.finditer(pattern, text):
            name = self._normalize_entity_name(match.group(1))
            if not self._is_valid_entity(name):
                continue

            # Guess type based on common patterns
            if any(word in name.lower() for word in ["inc", "corp", "ltd", "company", "co"]):
                entity_type = EntityType.ORGANIZATION
            elif any(word in name.lower() for word in ["city", "country", "state", "street"]):
                entity_type = EntityType.LOCATION
            else:
                entity_type = EntityType.OTHER

            entity = Entity.create(
                name=name,
                entity_type=entity_type.value,
                description=f"{entity_type.value}: {name}",
                source_unit_id=text_unit_id
            )
            entities.append(entity)
            entity_positions.append((name, entity_type.value, match.start()))

        return entities, entity_positions

    def _infer_relationships_from_cooccurrence(
        self,
        entities: List[Entity],
        entity_positions: List[Tuple[str, str, int]],
        text_unit_id: str,
        window_size: int = 200
    ) -> List[Relationship]:
        """
        Infer relationships from entity co-occurrence.

        Entities that appear close together in text are likely related.
        """
        relationships = []

        # Build position-based entity lookup
        positioned_entities: List[Tuple[Entity, int]] = []
        name_to_entity = {e.name.lower(): e for e in entities}

        for name, etype, pos in entity_positions:
            entity = name_to_entity.get(name.lower())
            if entity:
                positioned_entities.append((entity, pos))

        # Sort by position
        positioned_entities.sort(key=lambda x: x[1])

        # Find co-occurring entities within window
        seen_pairs: Set[Tuple[str, str]] = set()
        for i, (entity1, pos1) in enumerate(positioned_entities):
            for j in range(i + 1, len(positioned_entities)):
                entity2, pos2 = positioned_entities[j]

                # Stop if outside window
                if pos2 - pos1 > window_size:
                    break

                # Skip self-relationships
                if entity1.id == entity2.id:
                    continue

                # Create consistent pair key (alphabetical order)
                pair_key = tuple(sorted([entity1.id, entity2.id]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Calculate strength based on proximity
                distance = pos2 - pos1
                strength = max(0.3, 1.0 - (distance / window_size))

                # Determine relationship type based on entity types
                rel_type = self._infer_relationship_type(entity1.type, entity2.type)

                relationship = Relationship.create(
                    source_id=entity1.id,
                    target_id=entity2.id,
                    rel_type=rel_type,
                    description=f"{entity1.name} is related to {entity2.name}",
                    strength=strength,
                    source_unit_id=text_unit_id
                )
                relationships.append(relationship)

        return relationships

    def _infer_relationship_type(self, type1: str, type2: str) -> str:
        """Infer relationship type based on entity types."""
        types = {type1, type2}

        if EntityType.PERSON.value in types:
            if EntityType.ORGANIZATION.value in types:
                return RelationshipType.WORKS_FOR.value
            if EntityType.LOCATION.value in types:
                return RelationshipType.LOCATED_IN.value

        if EntityType.ORGANIZATION.value in types:
            if EntityType.LOCATION.value in types:
                return RelationshipType.LOCATED_IN.value
            if EntityType.PRODUCT.value in types:
                return RelationshipType.PRODUCES.value

        if EntityType.CONCEPT.value in types:
            return RelationshipType.RELATED_TO.value

        return RelationshipType.RELATED_TO.value

    def _deduplicate_entities(self, entities: List[Entity]) -> List[Entity]:
        """Deduplicate entities by normalized name."""
        seen: Dict[str, Entity] = {}
        for entity in entities:
            key = entity.name.lower().strip()
            if key in seen:
                # Merge with existing
                seen[key] = seen[key].merge_with(entity)
            else:
                seen[key] = entity
        return list(seen.values())

    def extract_single(self, text: str, doc_id: str = "default") -> ExtractionResult:
        """Extract entities and relationships from a single text."""
        text_unit_id = f"{doc_id}_0"

        # Extract entities
        if self._nlp:
            entities, positions = self._extract_with_spacy(text, text_unit_id)
        else:
            entities, positions = self._extract_with_regex(text, text_unit_id)

        # Deduplicate
        entities = self._deduplicate_entities(entities)

        # Infer relationships from co-occurrence
        relationships = self._infer_relationships_from_cooccurrence(
            entities, positions, text_unit_id
        )

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            source_units=[text_unit_id],
            metadata={"extractor": "nlp", "model": self.model_name}
        )

    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """Extract entities and relationships from multiple text units."""
        if not text_units:
            return ExtractionResult()

        all_entities = []
        all_relationships = []
        all_source_units = []

        for unit in text_units:
            # Extract from each unit
            if self._nlp:
                entities, positions = self._extract_with_spacy(unit.text, unit.id)
            else:
                entities, positions = self._extract_with_regex(unit.text, unit.id)

            # Infer relationships within this unit
            relationships = self._infer_relationships_from_cooccurrence(
                entities, positions, unit.id
            )

            all_entities.extend(entities)
            all_relationships.extend(relationships)
            all_source_units.append(unit.id)

        # Deduplicate entities across all units
        all_entities = self._deduplicate_entities(all_entities)

        # Update relationship IDs to point to deduplicated entities
        entity_name_to_id = {e.name.lower(): e.id for e in all_entities}
        updated_relationships = []
        for rel in all_relationships:
            # Try to find the entities by name
            source_entity = next((e for e in all_entities if e.id == rel.source_id), None)
            target_entity = next((e for e in all_entities if e.id == rel.target_id), None)

            if source_entity and target_entity:
                rel.source_id = source_entity.id
                rel.target_id = target_entity.id
                updated_relationships.append(rel)

        # Deduplicate relationships
        rel_lookup: Dict[Tuple[str, str, str], Relationship] = {}
        for rel in updated_relationships:
            key = (rel.source_id, rel.target_id, rel.type)
            if key in rel_lookup:
                rel_lookup[key] = rel_lookup[key].merge_with(rel)
            else:
                rel_lookup[key] = rel

        return ExtractionResult(
            entities=all_entities,
            relationships=list(rel_lookup.values()),
            source_units=all_source_units,
            metadata={"extractor": "nlp", "model": self.model_name}
        )

    def extract_batch(self, text_units: List[TextUnit], batch_size: int = 50) -> ExtractionResult:
        """Extract from text units in batches with parallel processing."""
        if not text_units:
            return ExtractionResult()

        # For spaCy, use pipe for efficient batch processing
        if self._nlp and len(text_units) > batch_size:
            return self._extract_with_pipe(text_units)

        # Fall back to regular extraction
        return self.extract(text_units)

    def _extract_with_pipe(self, text_units: List[TextUnit]) -> ExtractionResult:
        """Use spaCy's pipe for efficient batch processing."""
        texts = [unit.text for unit in text_units]
        unit_ids = [unit.id for unit in text_units]

        all_entities = []
        all_relationships = []

        # Process in batches using spaCy's pipe
        for i, doc in enumerate(self._nlp.pipe(texts, batch_size=50)):
            unit_id = unit_ids[i]

            entities = []
            positions = []

            # Extract named entities
            for ent in doc.ents:
                name = self._normalize_entity_name(ent.text)
                if not self._is_valid_entity(name):
                    continue

                entity_type = self.SPACY_TO_ENTITY_TYPE.get(ent.label_, EntityType.OTHER)
                entity = Entity.create(
                    name=name,
                    entity_type=entity_type.value,
                    description=f"{entity_type.value}: {name}",
                    source_unit_id=unit_id
                )
                entities.append(entity)
                positions.append((name, entity_type.value, ent.start_char))

            # Infer relationships
            relationships = self._infer_relationships_from_cooccurrence(
                entities, positions, unit_id
            )

            all_entities.extend(entities)
            all_relationships.extend(relationships)

        # Deduplicate
        all_entities = self._deduplicate_entities(all_entities)

        # Deduplicate relationships
        rel_lookup: Dict[Tuple[str, str, str], Relationship] = {}
        for rel in all_relationships:
            key = (rel.source_id, rel.target_id, rel.type)
            if key in rel_lookup:
                rel_lookup[key] = rel_lookup[key].merge_with(rel)
            else:
                rel_lookup[key] = rel

        return ExtractionResult(
            entities=all_entities,
            relationships=list(rel_lookup.values()),
            source_units=unit_ids,
            metadata={"extractor": "nlp", "model": self.model_name}
        )


def create_nlp_extractor(config: Optional[GraphRAGConfig] = None) -> NLPExtractor:
    """Factory function to create an NLP extractor."""
    if config:
        return NLPExtractor(model=config.nlp_model, config=config)
    return NLPExtractor()
