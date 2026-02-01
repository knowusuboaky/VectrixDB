"""
Entity Extraction Module for VectrixDB GraphRAG.

Provides multiple extraction strategies:
- SpacyRebelExtractor: spaCy NER + mREBEL relations (RECOMMENDED - best accuracy, no LLM)
- REBELExtractor: mREBEL model only - bundled, no LLM costs, 18 languages
- NLPExtractor: Fast, free extraction using spaCy NER
- LLMExtractor: High-quality extraction using LLMs
- HybridExtractor: NLP for speed + LLM for quality (costs money)

Example:
    >>> from vectrixdb.core.graphrag.extractor import create_extractor
    >>>
    >>> # spaCy + mREBEL (RECOMMENDED - best accuracy, no LLM)
    >>> extractor = create_extractor("spacy_rebel")
    >>>
    >>> # mREBEL-only extraction
    >>> extractor = create_extractor("rebel")
    >>>
    >>> # NLP-only (fast, free, lower quality)
    >>> extractor = create_extractor("nlp")
    >>>
    >>> # LLM-only (highest quality, costs money)
    >>> extractor = create_extractor("llm", provider="openai", model="gpt-4o-mini")
    >>>
    >>> # Hybrid: NLP + LLM for important chunks
    >>> extractor = create_extractor("hybrid")
"""

from .base import (
    BaseExtractor,
    Entity,
    Relationship,
    ExtractionResult,
    EntityType,
    RelationshipType,
)

from .nlp_extractor import NLPExtractor, create_nlp_extractor
from .llm_extractor import LLMExtractor, create_llm_extractor
from .rebel_extractor import REBELExtractor, create_rebel_extractor
from .hybrid_extractor import HybridExtractor as SpacyRebelExtractor, create_hybrid_extractor as create_spacy_rebel_extractor
from .hybrid_extractor import HybridExtractor  # Also export as HybridExtractor for backward compatibility

from typing import Optional, List
from ..config import GraphRAGConfig, ExtractorType
from ..chunker import TextUnit


class HybridLLMExtractor(BaseExtractor):
    """
    Hybrid extractor combining NLP speed with LLM quality.

    Strategy:
    1. Use NLP extraction on all text units (fast, free)
    2. Identify "important" chunks with many entities
    3. Re-extract important chunks with LLM (high quality)
    4. Merge results, preferring LLM extractions

    This gives ~80% of LLM quality at ~20% of the cost.
    """

    def __init__(
        self,
        config: Optional[GraphRAGConfig] = None,
        importance_threshold: float = 0.3,
        min_entities_for_llm: int = 3,
    ):
        """
        Initialize hybrid extractor.

        Args:
            config: GraphRAGConfig for LLM and NLP settings.
            importance_threshold: Fraction of chunks to process with LLM (0.0-1.0).
            min_entities_for_llm: Minimum NLP entities to trigger LLM extraction.
        """
        self.config = config or GraphRAGConfig()
        self.importance_threshold = importance_threshold
        self.min_entities_for_llm = min_entities_for_llm

        self.nlp_extractor = NLPExtractor(config=self.config)

        # Only initialize LLM if API key is available or using Ollama
        self._llm_extractor = None
        if self.config.llm_provider.value == "ollama" or self.config.llm_api_key:
            try:
                self._llm_extractor = LLMExtractor(config=self.config)
            except Exception:
                # LLM not available, will use NLP only
                pass

    def _identify_important_units(
        self,
        text_units: List[TextUnit],
        nlp_result: ExtractionResult
    ) -> List[TextUnit]:
        """Identify chunks that should be processed with LLM."""
        # Count entities per text unit
        unit_entity_counts = {}
        for entity in nlp_result.entities:
            for source_id in entity.source_units:
                unit_entity_counts[source_id] = unit_entity_counts.get(source_id, 0) + 1

        # Sort by entity count (descending)
        sorted_units = sorted(
            [(unit, unit_entity_counts.get(unit.id, 0)) for unit in text_units],
            key=lambda x: -x[1]
        )

        # Select top N% with at least min_entities
        num_to_select = max(1, int(len(text_units) * self.importance_threshold))
        important_units = [
            unit for unit, count in sorted_units[:num_to_select]
            if count >= self.min_entities_for_llm
        ]

        return important_units

    def extract_single(self, text: str, doc_id: str = "default") -> ExtractionResult:
        """Extract from a single text using hybrid approach."""
        # For single texts, just use NLP (LLM would be overkill)
        nlp_result = self.nlp_extractor.extract_single(text, doc_id)

        # If LLM available and text has many entities, enhance with LLM
        if self._llm_extractor and len(nlp_result.entities) >= self.min_entities_for_llm:
            try:
                llm_result = self._llm_extractor.extract_single(text, doc_id)
                # Merge, preferring LLM results
                return llm_result.merge_with(nlp_result)
            except Exception:
                pass

        return nlp_result

    def extract(self, text_units: List[TextUnit]) -> ExtractionResult:
        """Extract from multiple text units using hybrid approach."""
        if not text_units:
            return ExtractionResult()

        # Stage 1: NLP extraction on all units (fast)
        nlp_result = self.nlp_extractor.extract(text_units)

        # If no LLM available, return NLP result
        if not self._llm_extractor:
            nlp_result.metadata["extractor"] = "hybrid_nlp_only"
            return nlp_result

        # Stage 2: Identify important chunks
        important_units = self._identify_important_units(text_units, nlp_result)

        if not important_units:
            nlp_result.metadata["extractor"] = "hybrid_nlp_only"
            return nlp_result

        # Stage 3: LLM extraction on important chunks
        try:
            llm_result = self._llm_extractor.extract(important_units)
        except Exception:
            nlp_result.metadata["extractor"] = "hybrid_nlp_only"
            return nlp_result

        # Stage 4: Merge results
        # LLM results take precedence (merge LLM into NLP)
        merged = nlp_result.merge_with(llm_result)
        merged.metadata = {
            "extractor": "hybrid",
            "nlp_units": len(text_units),
            "llm_units": len(important_units),
        }

        return merged

    def extract_batch(self, text_units: List[TextUnit], batch_size: int = 50) -> ExtractionResult:
        """Extract in batches."""
        return self.extract(text_units)


def create_extractor(
    extractor_type: str = "spacy_rebel",
    config: Optional[GraphRAGConfig] = None,
    **kwargs
) -> BaseExtractor:
    """
    Factory function to create an entity extractor.

    Args:
        extractor_type: Type of extractor:
            - "spacy_rebel": spaCy NER + mREBEL relations (RECOMMENDED, default)
            - "rebel": mREBEL only
            - "nlp": spaCy NLP only
            - "llm": LLM only (costs money)
            - "hybrid": NLP + LLM (costs money)
        config: Optional GraphRAGConfig.
        **kwargs: Additional arguments passed to the extractor.

    Returns:
        An instance of BaseExtractor.
    """
    if config:
        extractor_type = config.extractor.value if isinstance(config.extractor, ExtractorType) else config.extractor

    if extractor_type == "spacy_rebel":
        return SpacyRebelExtractor(config=config, **kwargs)
    elif extractor_type == "rebel":
        return REBELExtractor(config=config, **kwargs)
    elif extractor_type == "nlp":
        return NLPExtractor(config=config, **kwargs)
    elif extractor_type == "llm":
        return LLMExtractor(config=config, **kwargs)
    elif extractor_type == "hybrid":
        return HybridLLMExtractor(config=config, **kwargs)
    else:
        raise ValueError(f"Unknown extractor type: {extractor_type}")


__all__ = [
    # Base classes
    "BaseExtractor",
    "Entity",
    "Relationship",
    "ExtractionResult",
    "EntityType",
    "RelationshipType",

    # Extractors
    "SpacyRebelExtractor",  # RECOMMENDED: spaCy NER + mREBEL relations
    "HybridExtractor",  # Alias for SpacyRebelExtractor (backward compatibility)
    "REBELExtractor",
    "NLPExtractor",
    "LLMExtractor",
    "HybridLLMExtractor",

    # Factory functions
    "create_extractor",
    "create_spacy_rebel_extractor",
    "create_rebel_extractor",
    "create_nlp_extractor",
    "create_llm_extractor",
]
