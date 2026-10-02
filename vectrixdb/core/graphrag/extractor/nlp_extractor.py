"""
NLP-based Entity Extractor for GraphRAG.

Uses spaCy for fast, free entity extraction without requiring an LLM.
Extracts named entities and infers relationships from co-occurrence.
"""

import logging
import re
from collections import defaultdict
from typing import List, Dict, Set, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import (
    Confidence,
    BaseExtractor,
    Entity,
    Relationship,
    ExtractionResult,
    EntityType,
    RelationshipType,
)
from ..chunker import TextUnit
from ..config import GraphRAGConfig


__all__ = [
    "NLPExtractor",
    "create_nlp_extractor",
]


# ============================================================================
# SETTINGS: the logger, and whether the fallback was warned about
# ============================================================================
#
# One logger, and a flag so a missing spaCy model is warned about once, not
# once a chunk.

logger = logging.getLogger(__name__)

#: The regex fallback is announced once per process, not per extractor.
_FALLBACK_WARNED = False


# ============================================================================
# THE NLP EXTRACTOR: spaCy alone
# ============================================================================
#
# INPUT   text units
# OUTPUT  named entities, and relationships inferred from co-occurrence; a
#         factory
#
# Fast and free; relationships are guesses.


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
        """Load the spaCy model, or fall back to the regex extractor and say so."""
        global _FALLBACK_WARNED
        try:
            import spacy

            try:
                self._nlp = spacy.load(self.model_name)
            except OSError:
                from vectrixdb._net import auto_download_allowed

                if not auto_download_allowed():
                    # spaCy is installed but its model is not. Fetching it here
                    # would be a download nobody asked for, so fall back and say
                    # exactly what to run.
                    self._nlp = None
                    logger.warning(
                        "spaCy is installed but the %s model is not, so graph mode is "
                        "using the regex extractor. Run: python -m spacy download %s "
                        "(or set VECTRIXDB_AUTO_DOWNLOAD=1 to fetch it on first use).",
                        self.model_name,
                        self.model_name,
                    )
                    return
                logger.info("Downloading spaCy model %s, as allowed", self.model_name)
                spacy.cli.download(self.model_name)
                self._nlp = spacy.load(self.model_name)
        except ImportError:
            self._nlp = None
            # Once per process, at warning: a print() went to stdout on every
            # extractor construction and said nothing about what to do.
            if not _FALLBACK_WARNED:
                _FALLBACK_WARNED = True
                logger.warning(
                    "spaCy is not installed, so graph mode is using the regex "
                    "extractor, which only sees capitalised phrases, acronyms, "
                    "quoted terms and repeated phrases. For real entity "
                    'extraction: pip install "vectrixdb[nlp]" and '
                    "python -m spacy download %s",
                    self.model_name,
                )

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

    def _extract_with_spacy(
        self, text: str, text_unit_id: str
    ) -> Tuple[List[Entity], List[Tuple[str, str, int]]]:
        """Extract entities using spaCy."""
        nlp = self._nlp
        if nlp is None:
            raise RuntimeError("spaCy model is not loaded")
        doc = nlp(text)
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
                source_unit_id=text_unit_id,
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
                if len(name.split()) >= 2 or (
                    chunk.root.pos_ in {"NOUN", "PROPN"} and len(name) > 5
                ):
                    entity = Entity.create(
                        name=name,
                        entity_type=EntityType.CONCEPT.value,
                        description=f"Concept: {name}",
                        source_unit_id=text_unit_id,
                    )
                    entities.append(entity)
                    entity_positions.append((name, EntityType.CONCEPT.value, chunk.start_char))
                    seen_names.add(name.lower())

        return entities, entity_positions

    #: Words that never begin or end a concept phrase in the regex fallback.
    _PHRASE_STOPWORDS = frozenset(
        "a an the and or but if then of to in on at for from by with as is are was "
        "were be been being it its this that these those i you he she we they my "
        "your our their not no so do does did have has had can could will would "
        "should may might must when where what which who how about into over "
        "under also very just than there here again back out".split()
    )

    def _extract_with_regex(
        self, text: str, text_unit_id: str
    ) -> Tuple[List[Entity], List[Tuple[str, str, int]]]:
        """Fallback extraction when spaCy is not installed.

        Capitalised phrases used to be the whole of this, so a lowercase corpus
        (chat, tickets, notes) extracted nothing and graph mode silently had
        no graph. Three more sources give it something to work with: acronyms,
        quoted terms, and lowercase phrases that recur within the text. It is
        still a heuristic; the warning at load time says how to get a model.
        """
        entities: List[Entity] = []
        positions: List[Tuple[str, str, int]] = []
        seen: List[str] = []

        def emit(raw: str, entity_type: EntityType, start: int) -> None:
            name = self._normalize_entity_name(raw)
            key = name.lower()
            if not self._is_valid_entity(name) or any(key == s or key in s for s in seen):
                return
            seen.append(key)
            entity = Entity.create(
                name=name,
                entity_type=entity_type.value,
                description=f"{entity_type.value}: {name}",
                source_unit_id=text_unit_id,
            )
            entities.append(entity)
            positions.append((name, entity_type.value, start))

        # Capitalised phrases (likely proper nouns), typed by a few cue words.
        for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", text):
            name = match.group(1)
            lower = name.lower()
            if any(word in lower for word in ["inc", "corp", "ltd", "company", "co"]):
                entity_type = EntityType.ORGANIZATION
            elif any(word in lower for word in ["city", "country", "state", "street"]):
                entity_type = EntityType.LOCATION
            else:
                entity_type = EntityType.OTHER
            emit(name, entity_type, match.start())

        # Acronyms and product-style tokens: PDF, API, GPT4.
        for match in re.finditer(r"\b([A-Z][A-Z0-9]{1,9})\b", text):
            emit(match.group(1), EntityType.OTHER, match.start())

        # Quoted terms are things the writer chose to name.
        for match in re.finditer(r"[\"\u201c']([^\"\u201d']{2,60})[\"\u201d']", text):
            emit(match.group(1), EntityType.CONCEPT, match.start())

        # Lowercase phrases that recur. Adjacent tokens only, so a phrase never
        # spans punctuation, and no stopword at either end. Longer phrases are
        # counted first so a bigram inside an emitted trigram is absorbed.
        words = [(m.group(0), m.start()) for m in re.finditer(r"[a-z][a-z0-9\-]+", text)]
        for n in (3, 2):
            counts: Dict[str, int] = {}
            first_seen: Dict[str, int] = {}
            for i in range(len(words) - n + 1):
                gram = words[i : i + n]
                toks = [w for w, _ in gram]
                if toks[0] in self._PHRASE_STOPWORDS or toks[-1] in self._PHRASE_STOPWORDS:
                    continue
                if any(len(tok) < 3 for tok in toks):
                    continue
                if any(gram[j + 1][1] != gram[j][1] + len(gram[j][0]) + 1 for j in range(n - 1)):
                    continue
                phrase = " ".join(toks)
                counts[phrase] = counts.get(phrase, 0) + 1
                first_seen.setdefault(phrase, gram[0][1])
            for phrase, count in counts.items():
                if count >= 2:
                    emit(phrase, EntityType.CONCEPT, first_seen[phrase])

        positions.sort(key=lambda item: item[2])
        return entities, positions

    def _infer_relationships_from_cooccurrence(
        self,
        entities: List[Entity],
        entity_positions: List[Tuple[str, str, int]],
        text_unit_id: str,
        window_size: int = 200,
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
        seen_pairs: Set[Tuple[str, ...]] = set()
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
                    source_unit_id=text_unit_id,
                    # Proximity in a window, nothing more: the text never said
                    # these two are connected.
                    confidence=Confidence.AMBIGUOUS,
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
            metadata={"extractor": "nlp", "model": self.model_name},
        )

    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """Extract entities and relationships from multiple text units."""
        if not text_units:
            return ExtractionResult()

        all_entities: List[Entity] = []
        all_relationships: List[Relationship] = []
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

        # The name behind every id, taken before deduplication. A merged
        # entity keeps one id and loses the others, and a relationship that
        # pointed at a lost id used to be dropped on the floor rather than
        # remapped to the survivor.
        id_to_name = {e.id: e.name.lower() for e in all_entities}
        all_entities = self._deduplicate_entities(all_entities)

        entity_name_to_id = {e.name.lower(): e.id for e in all_entities}
        updated_relationships = []
        for rel in all_relationships:
            source_id = entity_name_to_id.get(id_to_name.get(rel.source_id, ""))
            target_id = entity_name_to_id.get(id_to_name.get(rel.target_id, ""))

            if source_id and target_id:
                rel.source_id = source_id
                rel.target_id = target_id
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
            metadata={"extractor": "nlp", "model": self.model_name},
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

        all_entities: List[Entity] = []
        all_relationships: List[Relationship] = []

        # Process in batches using spaCy's pipe
        nlp = self._nlp
        if nlp is None:
            raise RuntimeError("spaCy model is not loaded")
        for i, doc in enumerate(nlp.pipe(texts, batch_size=50)):
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
                    source_unit_id=unit_id,
                )
                entities.append(entity)
                positions.append((name, entity_type.value, ent.start_char))

            # Infer relationships
            relationships = self._infer_relationships_from_cooccurrence(
                entities, positions, unit_id
            )

            all_entities.extend(entities)
            all_relationships.extend(relationships)

        # Deduplicate, remapping relationship endpoints onto the entity
        # that survived rather than leaving them pointing at an id that is
        # no longer in the result.
        id_to_name = {e.id: e.name.lower() for e in all_entities}
        all_entities = self._deduplicate_entities(all_entities)
        surviving = {e.name.lower(): e.id for e in all_entities}
        remapped = []
        for rel in all_relationships:
            source_id = surviving.get(id_to_name.get(rel.source_id, ""))
            target_id = surviving.get(id_to_name.get(rel.target_id, ""))
            if source_id and target_id:
                rel.source_id = source_id
                rel.target_id = target_id
                remapped.append(rel)
        all_relationships = remapped

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
            metadata={"extractor": "nlp", "model": self.model_name},
        )


def create_nlp_extractor(config: Optional[GraphRAGConfig] = None) -> NLPExtractor:
    """Factory function to create an NLP extractor."""
    if config:
        return NLPExtractor(model=config.nlp_model, config=config)
    return NLPExtractor()
