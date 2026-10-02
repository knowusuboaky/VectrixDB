"""Unit tests for the GraphRAG entity extractors.

Covers vectrixdb/core/graphrag/extractor/base.py, nlp_extractor.py,
llm_extractor.py, hybrid_extractor.py and the create_extractor factory in
__init__.py.

Everything here runs offline. The LLM extractor is driven by a caller
supplied callable instead of a real provider client. The NLP and hybrid
extractors are built with Cls.__new__(Cls) so spaCy and mREBEL are never
actually loaded; where their loader methods are the thing under test, spaCy
and the model helpers in vectrixdb.models.embedded are replaced with small
fakes injected through sys.modules or monkeypatch.

Three real bugs were found while writing these tests and are documented in
place with pytest.mark.xfail(strict=True):

1. nlp_extractor.py: NLPExtractor.extract() and NLPExtractor._extract_with_pipe()
   both deduplicate entities across text units by name, but neither remaps
   relationships to the surviving (post merge) entity id. extract() drops the
   relationship outright; _extract_with_pipe() leaves it pointing at an
   entity id that no longer exists in the returned entities list.
2. base.py: ExtractionResult.merge_with() builds a brand new Relationship for
   anything not already present in self.relationships but forgets to carry
   over confidence (and valid_from/valid_to/superseded_by), so a stated,
   EXTRACTED relationship silently reverts to the INFERRED default the first
   time two ExtractionResults are merged.
3. llm_extractor.py: LLMExtractor.extract_batch() accepts a batch_size
   argument but never uses it. Every text unit is submitted to the thread
   pool in one go regardless of batch_size; only max_workers has any effect.
"""

from __future__ import annotations

import json
import logging
import sys
import types

import pytest

from vectrixdb.core.graphrag.chunker import TextUnit
from vectrixdb.core.graphrag.config import GraphRAGConfig, LLMProvider

from vectrixdb.core.graphrag.extractor.base import (
    BaseExtractor,
    Confidence,
    Entity,
    EntityType,
    ExtractionResult,
    Relationship,
    RelationshipType,
)
from vectrixdb.core.graphrag.extractor import hybrid_extractor as hybrid_mod
from vectrixdb.core.graphrag.extractor import llm_extractor as llm_mod
from vectrixdb.core.graphrag.extractor import nlp_extractor as nlp_mod
from vectrixdb.core.graphrag.extractor.hybrid_extractor import HybridExtractor
from vectrixdb.core.graphrag.extractor.llm_extractor import LLMExtractor, create_llm_extractor
from vectrixdb.core.graphrag.extractor.nlp_extractor import NLPExtractor, create_nlp_extractor
from vectrixdb.core.graphrag import extractor as extractor_pkg


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_unit(unit_id: str, text: str, doc_id: str = "doc1", position: int = 0) -> TextUnit:
    """Build a TextUnit with a token count derived from the text."""
    return TextUnit(
        id=unit_id, text=text, doc_id=doc_id, position=position, token_count=len(text.split())
    )


def _raiser(exc: Exception):
    def _inner(*_args, **_kwargs):
        raise exc

    return _inner


# --- Fake spaCy pieces (shared by the NLP and hybrid extractor tests) -----


class FakeEnt:
    def __init__(self, text: str, label: str, start: int = 0):
        self.text = text
        self.label_ = label
        self.start_char = start


class FakeChunk:
    def __init__(self, text: str, start: int, pos: str):
        self.text = text
        self.start_char = start
        self.root = types.SimpleNamespace(pos_=pos)


class FakeDoc:
    def __init__(self, ents=(), noun_chunks=()):
        self.ents = list(ents)
        self.noun_chunks = list(noun_chunks)


class FakeNLP:
    """Stands in for a loaded spaCy Language object."""

    def __init__(self, doc_factory=None, pipe_docs=None):
        self._doc_factory = doc_factory
        self._pipe_docs = list(pipe_docs or [])

    def __call__(self, text):
        return self._doc_factory(text)

    def pipe(self, texts, batch_size=50):
        return iter(self._pipe_docs)


def make_fake_spacy_module(load_fn, download_fn=None):
    """Build a stand in for the top level `spacy` module."""
    download_calls = []

    def cli_download(name):
        download_calls.append(name)
        if download_fn is not None:
            download_fn(name)

    module = types.SimpleNamespace(load=load_fn, cli=types.SimpleNamespace(download=cli_download))
    module.download_calls = download_calls
    return module


# --- Fake REBEL triplet, used by the hybrid extractor tests ---------------


class FakeTriplet:
    def __init__(self, head, head_type, relation, tail, tail_type):
        self.head = head
        self.head_type = head_type
        self.relation = relation
        self.tail = tail
        self.tail_type = tail_type


class FakeRebel:
    def __init__(self, triplets):
        self._triplets = list(triplets)
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return list(self._triplets)


# ---------------------------------------------------------------------------
# base.py: Entity
# ---------------------------------------------------------------------------


class TestEntity:
    def test_create_generates_id_and_defaults(self):
        entity = Entity.create("Alice", EntityType.PERSON.value, source_unit_id="u1")
        assert entity.id.startswith("entity_")
        assert entity.name == "Alice"
        assert entity.type == EntityType.PERSON.value
        assert entity.source_units == ["u1"]
        assert entity.aliases == set()

    def test_create_without_source_unit(self):
        entity = Entity.create("Bob", EntityType.PERSON.value)
        assert entity.source_units == []

    def test_equality_is_by_id_only(self):
        a = Entity(id="e1", name="A", type=EntityType.OTHER.value)
        b = Entity(id="e1", name="Different name", type=EntityType.CONCEPT.value)
        c = Entity(id="e2", name="A", type=EntityType.OTHER.value)
        assert a == b
        assert a != c
        assert a != "e1"
        assert hash(a) == hash(b)

    def test_merge_with_combines_descriptions(self):
        a = Entity(id="e1", name="Apple", type=EntityType.ORGANIZATION.value, description="A fruit")
        b = Entity(
            id="e2", name="Apple", type=EntityType.ORGANIZATION.value, description="A company"
        )
        merged = a.merge_with(b)
        assert merged.id == "e1"
        assert merged.description == "A fruit A company"

    def test_merge_with_skips_duplicate_description(self):
        a = Entity(id="e1", name="Apple", type=EntityType.OTHER.value, description="A big company")
        b = Entity(id="e2", name="Apple", type=EntityType.OTHER.value, description="big company")
        merged = a.merge_with(b)
        assert merged.description == "A big company"

    def test_merge_with_falls_back_when_one_description_is_empty(self):
        a = Entity(id="e1", name="Apple", type=EntityType.OTHER.value, description="")
        b = Entity(id="e2", name="Apple", type=EntityType.OTHER.value, description="A company")
        assert a.merge_with(b).description == "A company"
        assert b.merge_with(a).description == "A company"

    def test_merge_with_unions_source_units_and_aliases(self):
        a = Entity(id="e1", name="Apple", type=EntityType.OTHER.value, source_units=["u1"])
        b = Entity(
            id="e2",
            name="Apple Inc",
            type=EntityType.OTHER.value,
            source_units=["u2"],
            aliases={"AAPL"},
        )
        merged = a.merge_with(b)
        assert set(merged.source_units) == {"u1", "u2"}
        assert merged.aliases == {"AAPL", "Apple Inc"}

    def test_merge_with_takes_max_importance_and_merges_attributes(self):
        a = Entity(
            id="e1", name="Apple", type=EntityType.OTHER.value, importance=0.2, attributes={"x": 1}
        )
        b = Entity(
            id="e2", name="Apple", type=EntityType.OTHER.value, importance=0.7, attributes={"y": 2}
        )
        merged = a.merge_with(b)
        assert merged.importance == 0.7
        assert merged.attributes == {"x": 1, "y": 2}

    def test_merge_with_prefers_own_embedding(self):
        a = Entity(id="e1", name="Apple", type=EntityType.OTHER.value, embedding=[1.0])
        b = Entity(id="e2", name="Apple", type=EntityType.OTHER.value, embedding=[2.0])
        assert a.merge_with(b).embedding == [1.0]
        assert b.merge_with(a).embedding == [2.0]


# ---------------------------------------------------------------------------
# base.py: Relationship
# ---------------------------------------------------------------------------


class TestRelationship:
    def test_create_defaults_to_inferred(self):
        rel = Relationship.create("e1", "e2", RelationshipType.RELATED_TO.value)
        assert rel.id.startswith("rel_")
        assert rel.confidence == Confidence.INFERRED

    def test_equality_is_by_id_only(self):
        a = Relationship(id="r1", source_id="e1", target_id="e2", type="RELATED_TO")
        b = Relationship(id="r1", source_id="e9", target_id="e8", type="WORKS_FOR")
        assert a == b
        assert a != "r1"
        assert hash(a) == hash(b)

    def test_merge_with_weights_strength_by_source_unit_count(self):
        a = Relationship(
            id="r1",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            strength=0.2,
            source_units=["u1", "u2"],
        )
        b = Relationship(
            id="r2",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            strength=0.8,
            source_units=["u3"],
        )
        merged = a.merge_with(b)
        assert merged.strength == pytest.approx(0.4)

    def test_merge_with_falls_back_to_self_strength_when_no_source_units(self):
        a = Relationship(id="r1", source_id="e1", target_id="e2", type="RELATED_TO", strength=0.3)
        b = Relationship(id="r2", source_id="e1", target_id="e2", type="RELATED_TO", strength=0.9)
        assert a.merge_with(b).strength == pytest.approx(0.3)

    def test_merge_with_caps_strength_at_one(self):
        a = Relationship(
            id="r1",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            strength=1.0,
            source_units=["u1"],
        )
        b = Relationship(
            id="r2",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            strength=1.0,
            source_units=["u2"],
        )
        assert a.merge_with(b).strength == 1.0

    def test_merge_with_keeps_the_stronger_confidence(self):
        extracted = Relationship(
            id="r1",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            confidence=Confidence.AMBIGUOUS,
        )
        stated = Relationship(
            id="r2",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            confidence=Confidence.EXTRACTED,
        )
        assert extracted.merge_with(stated).confidence == Confidence.EXTRACTED
        assert stated.merge_with(extracted).confidence == Confidence.EXTRACTED

    def test_merge_with_combines_descriptions_and_bidirectional(self):
        a = Relationship(
            id="r1",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            description="works with",
            bidirectional=False,
        )
        b = Relationship(
            id="r2",
            source_id="e1",
            target_id="e2",
            type="RELATED_TO",
            description="collaborates with",
            bidirectional=True,
        )
        merged = a.merge_with(b)
        assert merged.description == "works with collaborates with"
        assert merged.bidirectional is True


# ---------------------------------------------------------------------------
# base.py: ExtractionResult
# ---------------------------------------------------------------------------


class TestExtractionResult:
    def test_counts_and_lookup_by_name_and_alias(self):
        alice = Entity(id="e1", name="Alice", type=EntityType.PERSON.value, aliases={"Al"})
        result = ExtractionResult(entities=[alice])
        assert result.entity_count == 1
        assert result.get_entity_by_name("ALICE") is alice
        assert result.get_entity_by_name("al") is alice
        assert result.get_entity_by_name("nobody") is None

    def test_lookup_by_id(self):
        alice = Entity(id="e1", name="Alice", type=EntityType.PERSON.value)
        result = ExtractionResult(entities=[alice])
        assert result.get_entity_by_id("e1") is alice
        assert result.get_entity_by_id("missing") is None

    def test_relationships_for_entity_matches_either_side(self):
        rel = Relationship(id="r1", source_id="e1", target_id="e2", type="RELATED_TO")
        result = ExtractionResult(relationships=[rel])
        assert result.relationship_count == 1
        assert result.get_relationships_for_entity("e1") == [rel]
        assert result.get_relationships_for_entity("e2") == [rel]
        assert result.get_relationships_for_entity("e3") == []

    def test_merge_with_merges_matching_entities_by_name(self):
        self_alice = Entity(
            id="eA1",
            name="Alice",
            type=EntityType.PERSON.value,
            description="Engineer",
            importance=0.2,
            attributes={"x": 1},
        )
        self_result = ExtractionResult(entities=[self_alice])

        other_alice = Entity(
            id="eA2",
            name="Alice",
            type=EntityType.PERSON.value,
            description="Works at Acme",
            importance=0.5,
            aliases={"Al"},
            attributes={"y": 2},
        )
        other_bob = Entity(id="eB1", name="Bob", type=EntityType.PERSON.value)
        other_result = ExtractionResult(entities=[other_alice, other_bob])

        merged = self_result.merge_with(other_result)

        assert merged.entity_count == 2
        alice = merged.get_entity_by_id("eA1")
        assert alice is not None
        assert alice.description == "Engineer Works at Acme"
        assert alice.importance == 0.5
        assert alice.aliases == {"Al", "Alice"}
        assert alice.attributes == {"x": 1, "y": 2}
        assert merged.get_entity_by_id("eB1") is not None
        # The other copy of Alice does not survive as its own entity.
        assert merged.get_entity_by_id("eA2") is None

    def test_merge_with_remaps_relationship_endpoints_to_the_surviving_entity(self):
        self_result = ExtractionResult(
            entities=[Entity(id="eA1", name="Alice", type=EntityType.PERSON.value)]
        )
        other_rel = Relationship(
            id="rel_o",
            source_id="eA2",
            target_id="eB1",
            type=RelationshipType.RELATED_TO.value,
            confidence=Confidence.INFERRED,
            strength=0.9,
        )
        other_result = ExtractionResult(
            entities=[
                Entity(id="eA2", name="Alice", type=EntityType.PERSON.value),
                Entity(id="eB1", name="Bob", type=EntityType.PERSON.value),
            ],
            relationships=[other_rel],
        )

        merged = self_result.merge_with(other_result)

        assert merged.relationship_count == 1
        new_rel = merged.relationships[0]
        assert new_rel.source_id == "eA1"  # remapped from eA2, the id that did not survive
        assert new_rel.target_id == "eB1"

    def test_merge_with_merges_relationship_when_key_already_exists(self):
        existing = Relationship(
            id="rel_s",
            source_id="eA1",
            target_id="eB1",
            type=RelationshipType.RELATED_TO.value,
            confidence=Confidence.INFERRED,
            strength=0.4,
            source_units=["u1"],
        )
        self_result = ExtractionResult(
            entities=[
                Entity(id="eA1", name="Alice", type=EntityType.PERSON.value),
                Entity(id="eB1", name="Bob", type=EntityType.PERSON.value),
            ],
            relationships=[existing],
        )
        other_rel = Relationship(
            id="rel_o",
            source_id="eA2",
            target_id="eB1",
            type=RelationshipType.RELATED_TO.value,
            confidence=Confidence.EXTRACTED,
            strength=0.8,
            source_units=["u2"],
        )
        other_result = ExtractionResult(
            entities=[Entity(id="eA2", name="Alice", type=EntityType.PERSON.value)],
            relationships=[other_rel],
        )

        merged = self_result.merge_with(other_result)

        assert merged.relationship_count == 1
        combined = merged.relationships[0]
        assert combined.id == "rel_s"
        # Went through Relationship.merge_with, so the stronger confidence wins.
        assert combined.confidence == Confidence.EXTRACTED
        assert combined.strength == pytest.approx((0.4 * 1 + 0.8 * 1) / 2)

    def test_merge_with_combines_metadata_preferring_other(self):
        self_result = ExtractionResult(metadata={"a": 1, "shared": "self"})
        other_result = ExtractionResult(metadata={"b": 2, "shared": "other"})
        merged = self_result.merge_with(other_result)
        assert merged.metadata == {"a": 1, "b": 2, "shared": "other"}

    def test_merge_with_preserves_confidence_for_a_brand_new_relationship(self):
        self_result = ExtractionResult(
            entities=[Entity(id="eA1", name="Alice", type=EntityType.PERSON.value)]
        )
        other_rel = Relationship(
            id="rel_o",
            source_id="eA1",
            target_id="eB1",
            type=RelationshipType.RELATED_TO.value,
            confidence=Confidence.EXTRACTED,
            strength=0.9,
        )
        other_result = ExtractionResult(
            entities=[
                Entity(id="eA1", name="Alice", type=EntityType.PERSON.value),
                Entity(id="eB1", name="Bob", type=EntityType.PERSON.value),
            ],
            relationships=[other_rel],
        )

        merged = self_result.merge_with(other_result)

        assert merged.relationships[0].confidence == Confidence.EXTRACTED


# ---------------------------------------------------------------------------
# base.py: BaseExtractor
# ---------------------------------------------------------------------------


class TestBaseExtractor:
    def test_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            BaseExtractor()

    def test_extract_batch_default_delegates_to_extract(self):
        calls = []

        class Dummy(BaseExtractor):
            def extract(self, text_units):
                calls.append(list(text_units))
                return ExtractionResult(source_units=[u.id for u in text_units])

            def extract_single(self, text, doc_id="default"):
                return ExtractionResult()

        units = [make_unit("u1", "hello there")]
        result = Dummy().extract_batch(units, batch_size=5)

        assert calls == [units]
        assert result.source_units == ["u1"]

    def test_abstract_method_bodies_are_harmless(self):
        # The ABC's own extract/extract_single are `pass` stubs; call them
        # directly (bypassing the subclass override) to exercise that body.
        class Dummy(BaseExtractor):
            def extract(self, text_units):
                return ExtractionResult()

            def extract_single(self, text, doc_id="default"):
                return ExtractionResult()

        dummy = Dummy()
        assert BaseExtractor.extract(dummy, []) is None
        assert BaseExtractor.extract_single(dummy, "text") is None


# ---------------------------------------------------------------------------
# nlp_extractor.py: _load_model
# ---------------------------------------------------------------------------


class TestNLPExtractorLoadModel:
    def test_spacy_missing_warns_once(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "spacy", None)
        monkeypatch.setattr(nlp_mod, "_FALLBACK_WARNED", False)
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor.model_name = "en_core_web_sm"

        with caplog.at_level(logging.WARNING):
            extractor._load_model()

        assert extractor._nlp is None
        assert "spaCy is not installed" in caplog.text
        assert nlp_mod._FALLBACK_WARNED is True

    def test_spacy_missing_warns_only_the_first_time(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "spacy", None)
        monkeypatch.setattr(nlp_mod, "_FALLBACK_WARNED", True)
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor.model_name = "en_core_web_sm"

        with caplog.at_level(logging.WARNING):
            extractor._load_model()

        assert extractor._nlp is None
        assert caplog.text == ""

    def test_spacy_installed_and_model_loads(self, monkeypatch):
        fake_nlp = object()
        fake_spacy = make_fake_spacy_module(load_fn=lambda name: fake_nlp)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor.model_name = "en_core_web_sm"

        extractor._load_model()

        assert extractor._nlp is fake_nlp
        assert fake_spacy.download_calls == []

    def test_model_missing_without_auto_download_falls_back(self, monkeypatch, caplog):
        fake_spacy = make_fake_spacy_module(load_fn=_raiser(OSError("no model")))
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.delenv("VECTRIXDB_AUTO_DOWNLOAD", raising=False)
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor.model_name = "en_core_web_sm"

        with caplog.at_level(logging.WARNING):
            extractor._load_model()

        assert extractor._nlp is None
        assert fake_spacy.download_calls == []
        assert "is not, so graph mode is" in caplog.text

    def test_model_missing_with_auto_download_allowed_downloads_and_loads(self, monkeypatch):
        calls = {"n": 0}
        fake_nlp = object()

        def fake_load(name):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("no model")
            return fake_nlp

        fake_spacy = make_fake_spacy_module(load_fn=fake_load)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor.model_name = "en_core_web_sm"

        extractor._load_model()

        assert extractor._nlp is fake_nlp
        assert fake_spacy.download_calls == ["en_core_web_sm"]
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# nlp_extractor.py: small helpers
# ---------------------------------------------------------------------------


class TestNLPExtractorHelpers:
    @pytest.fixture
    def extractor(self):
        e = NLPExtractor.__new__(NLPExtractor)
        e.min_entity_length = 2
        e.max_entity_length = 100
        return e

    def test_normalize_collapses_whitespace_and_strips_punctuation(self, extractor):
        assert extractor._normalize_entity_name("  Marie   Curie ") == "Marie Curie"
        assert extractor._normalize_entity_name('"Apple."') == "Apple"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("", False),
            ("x", False),  # single char
            ("42", False),  # pure digits
            ("the", False),  # stopword
            ("THE", False),  # stopword, case-insensitive
            ("ok", True),
            ("a" * 101, False),  # too long
        ],
    )
    def test_is_valid_entity(self, extractor, name, expected):
        assert extractor._is_valid_entity(name) is expected

    @pytest.mark.parametrize(
        "type1,type2,expected",
        [
            (
                EntityType.PERSON.value,
                EntityType.ORGANIZATION.value,
                RelationshipType.WORKS_FOR.value,
            ),
            (EntityType.PERSON.value, EntityType.LOCATION.value, RelationshipType.LOCATED_IN.value),
            (
                EntityType.ORGANIZATION.value,
                EntityType.LOCATION.value,
                RelationshipType.LOCATED_IN.value,
            ),
            (
                EntityType.ORGANIZATION.value,
                EntityType.PRODUCT.value,
                RelationshipType.PRODUCES.value,
            ),
            (EntityType.CONCEPT.value, EntityType.OTHER.value, RelationshipType.RELATED_TO.value),
            (EntityType.PERSON.value, EntityType.PERSON.value, RelationshipType.RELATED_TO.value),
            (EntityType.OTHER.value, EntityType.OTHER.value, RelationshipType.RELATED_TO.value),
        ],
    )
    def test_infer_relationship_type(self, extractor, type1, type2, expected):
        assert extractor._infer_relationship_type(type1, type2) == expected

    def test_deduplicate_entities_merges_same_name(self, extractor):
        a = Entity(id="e1", name="Apple", type=EntityType.OTHER.value, source_units=["u1"])
        b = Entity(id="e2", name="apple", type=EntityType.OTHER.value, source_units=["u2"])
        c = Entity(id="e3", name="Orange", type=EntityType.OTHER.value)
        result = extractor._deduplicate_entities([a, b, c])
        by_name = {e.name.lower(): e for e in result}
        assert len(result) == 2
        assert by_name["apple"].id == "e1"
        assert set(by_name["apple"].source_units) == {"u1", "u2"}


# ---------------------------------------------------------------------------
# nlp_extractor.py: co-occurrence relationship inference
# ---------------------------------------------------------------------------


class TestNLPExtractorCooccurrence:
    @pytest.fixture
    def extractor(self):
        return NLPExtractor.__new__(NLPExtractor)

    def test_entities_within_window_are_related(self, extractor):
        a = Entity.create("Alice", EntityType.PERSON.value, source_unit_id="u1")
        b = Entity.create("Acme", EntityType.ORGANIZATION.value, source_unit_id="u1")
        positions = [(a.name, a.type, 0), (b.name, b.type, 10)]
        rels = extractor._infer_relationships_from_cooccurrence(
            [a, b], positions, "u1", window_size=200
        )
        assert len(rels) == 1
        assert rels[0].confidence == Confidence.AMBIGUOUS
        assert {rels[0].source_id, rels[0].target_id} == {a.id, b.id}

    def test_entities_outside_window_are_not_related(self, extractor):
        a = Entity.create("Alice", EntityType.PERSON.value, source_unit_id="u1")
        b = Entity.create("Acme", EntityType.ORGANIZATION.value, source_unit_id="u1")
        positions = [(a.name, a.type, 0), (b.name, b.type, 500)]
        rels = extractor._infer_relationships_from_cooccurrence(
            [a, b], positions, "u1", window_size=200
        )
        assert rels == []

    def test_strength_decreases_with_distance_but_has_a_floor(self, extractor):
        a = Entity.create("Alice", EntityType.PERSON.value, source_unit_id="u1")
        b = Entity.create("Acme", EntityType.ORGANIZATION.value, source_unit_id="u1")
        near = extractor._infer_relationships_from_cooccurrence(
            [a, b], [(a.name, a.type, 0), (b.name, b.type, 10)], "u1", window_size=200
        )[0]
        far = extractor._infer_relationships_from_cooccurrence(
            [a, b], [(a.name, a.type, 0), (b.name, b.type, 190)], "u1", window_size=200
        )[0]
        assert near.strength > far.strength
        assert far.strength >= 0.3

    def test_same_entity_twice_is_not_related_to_itself(self, extractor):
        a = Entity.create("Alice", EntityType.PERSON.value, source_unit_id="u1")
        positions = [(a.name, a.type, 0), (a.name, a.type, 5)]
        rels = extractor._infer_relationships_from_cooccurrence([a], positions, "u1")
        assert rels == []

    def test_each_pair_is_only_related_once(self, extractor):
        a = Entity.create("Alice", EntityType.PERSON.value, source_unit_id="u1")
        b = Entity.create("Acme", EntityType.ORGANIZATION.value, source_unit_id="u1")
        # Three mentions of the pair, all close together.
        positions = [
            (a.name, a.type, 0),
            (b.name, b.type, 5),
            (a.name, a.type, 10),
            (b.name, b.type, 15),
        ]
        rels = extractor._infer_relationships_from_cooccurrence([a, b], positions, "u1")
        assert len(rels) == 1


# ---------------------------------------------------------------------------
# nlp_extractor.py: spaCy driven extraction
# ---------------------------------------------------------------------------


class TestNLPExtractorRegexBranches:
    """Direct, self contained coverage of _extract_with_regex's branches.

    tests/unit/test_regex_extractor.py already exercises this method through
    extract_single; this class drives it directly so this file does not
    depend on that one for its own measured coverage.
    """

    @pytest.fixture
    def extractor(self):
        e = NLPExtractor.__new__(NLPExtractor)
        e.min_entity_length = 2
        e.max_entity_length = 100
        return e

    def test_cue_words_pick_organization_and_location(self, extractor):
        text = "Acme Corp is based in New York City. Acme Corp exports goods."
        entities, _positions = extractor._extract_with_regex(text, "u1")
        by_name = {e.name: e.type for e in entities}
        assert by_name["Acme Corp"] == EntityType.ORGANIZATION.value
        assert by_name["New York City"] == EntityType.LOCATION.value
        # "Acme Corp" repeats; the second mention is a duplicate and is skipped.
        assert [e.name for e in entities].count("Acme Corp") == 1

    def test_acronyms_and_quoted_terms(self, extractor):
        text = 'The team ships a PDF and calls the feature "dark mode" internally.'
        entities, _positions = extractor._extract_with_regex(text, "u1")
        by_name = {e.name: e.type for e in entities}
        assert by_name["PDF"] == EntityType.OTHER.value
        assert by_name["dark mode"] == EntityType.CONCEPT.value

    def test_repeated_lowercase_bigram_is_extracted(self, extractor):
        text = "deep learning models need data. deep learning data must be clean."
        entities, _positions = extractor._extract_with_regex(text, "u1")
        names = {e.name for e in entities}
        assert "deep learning" in names


class TestNLPExtractorSpacyPath:
    def test_extract_with_spacy_requires_a_loaded_model(self):
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = None
        with pytest.raises(RuntimeError):
            extractor._extract_with_spacy("text", "u1")

    def test_extract_with_spacy_maps_types_and_filters_noun_chunks(self):
        ents = [
            FakeEnt("Marie Curie", "PERSON", 0),
            FakeEnt("X", "PERSON", 20),  # too short, filtered
            FakeEnt("Weirdco", "MADE_UP_LABEL", 30),  # unmapped label -> OTHER
        ]
        chunks = [
            FakeChunk("Marie Curie", 0, "PROPN"),  # duplicate name, skipped
            FakeChunk("a", 5, "NOUN"),  # invalid, too short
            FakeChunk("radioactive elements", 40, "NOUN"),  # two words, qualifies
            FakeChunk("physics", 70, "NOUN"),  # single long noun, qualifies
            FakeChunk("it", 80, "PRON"),  # stopword, filtered
        ]
        doc = FakeDoc(ents=ents, noun_chunks=chunks)
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = FakeNLP(doc_factory=lambda text: doc)
        extractor.extract_noun_phrases = True
        extractor.min_entity_length = 2
        extractor.max_entity_length = 100

        entities, positions = extractor._extract_with_spacy("ignored", "u1")

        by_name = {e.name: e.type for e in entities}
        assert by_name["Marie Curie"] == EntityType.PERSON.value
        assert "X" not in by_name
        assert by_name["Weirdco"] == EntityType.OTHER.value
        assert by_name["radioactive elements"] == EntityType.CONCEPT.value
        assert by_name["physics"] == EntityType.CONCEPT.value
        assert "it" not in by_name
        assert len(entities) == len(positions) == 4

    def test_extract_with_spacy_skips_noun_phrases_when_disabled(self):
        doc = FakeDoc(
            ents=[FakeEnt("Acme Corp", "ORG", 0)],
            noun_chunks=[FakeChunk("some big idea", 20, "NOUN")],
        )
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = FakeNLP(doc_factory=lambda text: doc)
        extractor.extract_noun_phrases = False
        extractor.min_entity_length = 2
        extractor.max_entity_length = 100

        entities, _positions = extractor._extract_with_spacy("ignored", "u1")

        assert [e.name for e in entities] == ["Acme Corp"]
        assert entities[0].type == EntityType.ORGANIZATION.value

    def test_extract_with_pipe_requires_a_loaded_model(self):
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = None
        with pytest.raises(RuntimeError):
            extractor._extract_with_pipe([make_unit("u1", "text")])

    def test_extract_with_pipe_processes_each_document_and_dedupes(self):
        doc1 = FakeDoc(ents=[FakeEnt("Ada", "PERSON", 0), FakeEnt("Babbage", "PERSON", 10)])
        doc2 = FakeDoc(ents=[FakeEnt("Ada", "PERSON", 0)])
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = FakeNLP(pipe_docs=[doc1, doc2])
        extractor.model_name = "fake-model"
        extractor.min_entity_length = 2
        extractor.max_entity_length = 100
        units = [make_unit("u1", "Ada worked with Babbage."), make_unit("u2", "Ada again.")]

        result = extractor._extract_with_pipe(units)

        assert sorted(e.name for e in result.entities) == ["Ada", "Babbage"]
        assert result.source_units == ["u1", "u2"]
        assert result.metadata == {"extractor": "nlp", "model": "fake-model"}

    def test_extract_with_pipe_filters_invalid_entities(self):
        doc = FakeDoc(ents=[FakeEnt("Ada", "PERSON", 0), FakeEnt("X", "PERSON", 10)])
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = FakeNLP(pipe_docs=[doc])
        extractor.model_name = "fake-model"
        extractor.min_entity_length = 2
        extractor.max_entity_length = 100

        result = extractor._extract_with_pipe([make_unit("u1", "Ada and X.")])

        assert [e.name for e in result.entities] == ["Ada"]

    def test_extract_with_pipe_keeps_relationships_consistent_after_dedup(self):
        doc1 = FakeDoc(ents=[FakeEnt("Apple", "ORG", 0), FakeEnt("Steve", "PERSON", 10)])
        doc2 = FakeDoc(ents=[FakeEnt("Apple", "ORG", 0), FakeEnt("Tim", "PERSON", 10)])
        extractor = NLPExtractor.__new__(NLPExtractor)
        extractor._nlp = FakeNLP(pipe_docs=[doc1, doc2])
        extractor.model_name = "fake-model"
        # The validity filter reads these, so a bare __new__ object cannot
        # reach the code under test without them.
        extractor.min_entity_length = 2
        extractor.max_entity_length = 100
        extractor.extract_noun_phrases = False
        units = [make_unit("u1", "Apple and Steve."), make_unit("u2", "Apple and Tim.")]

        result = extractor._extract_with_pipe(units)

        # Apple appears in both units and is merged to one entity. Every
        # relationship must point at an entity that is still in the result.
        entity_ids = {e.id for e in result.entities}
        assert sorted(e.name for e in result.entities) == ["Apple", "Steve", "Tim"]
        assert result.relationships, "the fixture should produce relationships"
        for rel in result.relationships:
            assert rel.source_id in entity_ids
            assert rel.target_id in entity_ids


# ---------------------------------------------------------------------------
# nlp_extractor.py: extract_single / extract / extract_batch
# ---------------------------------------------------------------------------


class TestNLPExtractorPipeline:
    def _regex_extractor(self):
        e = NLPExtractor.__new__(NLPExtractor)
        e._nlp = None
        e.extract_noun_phrases = True
        e.min_entity_length = 2
        e.max_entity_length = 100
        e.model_name = "regex-fallback"
        return e

    def test_extract_single_uses_regex_fallback_when_no_spacy(self):
        extractor = self._regex_extractor()
        result = extractor.extract_single("Marie Curie worked in Paris.", doc_id="docX")
        assert result.source_units == ["docX_0"]
        assert result.metadata == {"extractor": "nlp", "model": "regex-fallback"}
        names = {e.name.lower() for e in result.entities}
        assert "marie curie" in names and "paris" in names

    def test_extract_single_uses_spacy_when_a_model_is_loaded(self):
        doc = FakeDoc(ents=[FakeEnt("Ada Lovelace", "PERSON", 0)])
        extractor = self._regex_extractor()
        extractor._nlp = FakeNLP(doc_factory=lambda text: doc)

        result = extractor.extract_single("Ada Lovelace wrote the first algorithm.", doc_id="docY")

        assert result.source_units == ["docY_0"]
        assert [e.name for e in result.entities] == ["Ada Lovelace"]

    def test_extract_empty_list_returns_default_result(self):
        extractor = self._regex_extractor()
        result = extractor.extract([])
        assert result.entities == []
        assert result.relationships == []
        assert result.metadata == {}

    def test_extract_multiple_units_dedupes_and_tracks_source_units(self):
        extractor = self._regex_extractor()
        units = [
            make_unit("u1", "Marie Curie won a Nobel Prize.", doc_id="d1"),
            make_unit("u2", "Marie Curie moved to Paris.", doc_id="d1"),
        ]
        result = extractor.extract(units)
        assert result.source_units == ["u1", "u2"]
        names = [e.name.lower() for e in result.entities]
        assert names.count("marie curie") == 1

    def test_extract_batch_empty_returns_default_result(self):
        extractor = self._regex_extractor()
        assert extractor.extract_batch([]).entities == []

    def test_extract_batch_below_threshold_uses_plain_extract(self, monkeypatch):
        extractor = self._regex_extractor()
        called = {}

        def fake_extract(units):
            called["units"] = units
            return ExtractionResult(source_units=[u.id for u in units])

        monkeypatch.setattr(extractor, "extract", fake_extract)
        units = [make_unit("u1", "hello")]

        result = extractor.extract_batch(units, batch_size=50)

        assert called["units"] == units
        assert result.source_units == ["u1"]

    def test_extract_batch_above_threshold_with_spacy_uses_pipe(self, monkeypatch):
        extractor = self._regex_extractor()
        extractor._nlp = FakeNLP(pipe_docs=[])
        called = {}

        def fake_pipe(units):
            called["units"] = units
            return ExtractionResult()

        monkeypatch.setattr(extractor, "_extract_with_pipe", fake_pipe)
        units = [make_unit(f"u{i}", "hello") for i in range(3)]

        extractor.extract_batch(units, batch_size=2)

        assert called["units"] == units

    def test_extract_keeps_relationships_after_cross_unit_entity_merge(self, monkeypatch):
        extractor = self._regex_extractor()
        extractor._nlp = object()  # any truthy value selects the spaCy branch

        def fake_extract_with_spacy(text, unit_id):
            if unit_id == "u1":
                apple = Entity.create("Apple", EntityType.ORGANIZATION.value, source_unit_id="u1")
                steve = Entity.create("Steve", EntityType.PERSON.value, source_unit_id="u1")
                return (
                    [apple, steve],
                    [("Apple", apple.type, 0), ("Steve", steve.type, 10)],
                )
            apple = Entity.create("Apple", EntityType.ORGANIZATION.value, source_unit_id="u2")
            tim = Entity.create("Tim", EntityType.PERSON.value, source_unit_id="u2")
            return (
                [apple, tim],
                [("Apple", apple.type, 0), ("Tim", tim.type, 10)],
            )

        extractor._extract_with_spacy = fake_extract_with_spacy
        units = [make_unit("u1", "unused"), make_unit("u2", "unused")]

        result = extractor.extract(units)

        assert result.relationship_count == 2

    def test_extract_merges_relationships_that_collide_after_dedup(self):
        # Two units that both resolve to the exact same pair of entity objects
        # (as a shared entity-linking step further down the pipeline might
        # produce) hit extract()'s final relationship merge branch.
        extractor = self._regex_extractor()
        extractor._nlp = object()
        alice = Entity.create("Alice", EntityType.PERSON.value)
        bob = Entity.create("Bob", EntityType.PERSON.value)

        def fake_extract_with_spacy(text, unit_id):
            return ([alice, bob], [("Alice", alice.type, 0), ("Bob", bob.type, 10)])

        extractor._extract_with_spacy = fake_extract_with_spacy
        units = [make_unit("u1", "unused"), make_unit("u2", "unused")]

        result = extractor.extract(units)

        assert result.relationship_count == 1
        assert set(result.relationships[0].source_units) == {"u1", "u2"}


# ---------------------------------------------------------------------------
# nlp_extractor.py: factory
# ---------------------------------------------------------------------------


class TestCreateNLPExtractor:
    def test_default(self):
        extractor = create_nlp_extractor()
        assert isinstance(extractor, NLPExtractor)

    def test_with_config_uses_configured_model(self):
        config = GraphRAGConfig(nlp_model="en_core_web_md")
        extractor = create_nlp_extractor(config)
        assert extractor.model_name == "en_core_web_md"


# ---------------------------------------------------------------------------
# llm_extractor.py: provider dispatch in __init__ / _init_client
# ---------------------------------------------------------------------------


class TestLLMExtractorInit:
    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            LLMExtractor(provider="not-a-real-provider")

    def test_config_overrides_direct_arguments(self):
        config = GraphRAGConfig(
            llm_provider=LLMProvider.OLLAMA,
            llm_model="llama3.2",
            llm_temperature=0.5,
            llm_max_tokens=256,
        )
        extractor = LLMExtractor(provider="openai", model="ignored", config=config)
        assert extractor.provider == "ollama"
        assert extractor.model == "llama3.2"
        assert extractor.temperature == 0.5
        assert extractor.max_tokens == 256

    def test_openai_without_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        fake_openai = types.SimpleNamespace(OpenAI=lambda **kw: object())
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        with pytest.raises(ValueError, match="OpenAI API key not found"):
            LLMExtractor(provider="openai")

    def test_openai_missing_package_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(ImportError, match="openai package not installed"):
            LLMExtractor(provider="openai", api_key="test-key")

    def test_openai_success_builds_client_from_fake_package(self, monkeypatch):
        created = {}

        class FakeOpenAIClient:
            def __init__(self, **kwargs):
                created.update(kwargs)

        fake_openai = types.SimpleNamespace(OpenAI=FakeOpenAIClient)
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        extractor = LLMExtractor(provider="openai", api_key="test-key")

        assert isinstance(extractor._client, FakeOpenAIClient)
        assert created == {"api_key": "test-key"}
        assert extractor._call_fn == extractor._call_openai

    def test_ollama_package_present_is_used_directly(self, monkeypatch):
        fake_ollama = types.SimpleNamespace(generate=lambda **kw: {"response": "ok"})
        monkeypatch.setitem(sys.modules, "ollama", fake_ollama)

        extractor = LLMExtractor(provider="ollama", endpoint="http://custom:1234")

        assert extractor._client is fake_ollama
        assert extractor._endpoint == "http://custom:1234"
        assert extractor._call_fn == extractor._call_ollama

    def test_ollama_falls_back_to_plain_http_when_package_missing(self, monkeypatch):
        """Without the ollama package, the standard library. It needed requests, which the package never declared."""
        monkeypatch.setitem(sys.modules, "ollama", None)
        monkeypatch.setitem(sys.modules, "requests", None)

        extractor = LLMExtractor(provider="ollama")

        assert extractor._client is None
        assert extractor._endpoint == "http://localhost:11434"
        assert extractor._call_fn == extractor._call_ollama_http

    def test_bedrock_missing_package_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(ImportError, match="boto3 package not installed"):
            LLMExtractor(provider="aws_bedrock")

    def test_bedrock_success_builds_client_from_fake_package(self, monkeypatch):
        fake_client = object()
        created = {}

        def fake_client_factory(name):
            created["name"] = name
            return fake_client

        fake_boto3 = types.SimpleNamespace(client=fake_client_factory)
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

        extractor = LLMExtractor(provider="aws_bedrock")

        assert created == {"name": "bedrock-runtime"}
        assert extractor._client is fake_client
        assert extractor._call_fn == extractor._call_bedrock

    def test_azure_without_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        fake_openai = types.SimpleNamespace(AzureOpenAI=lambda **kw: object())
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        with pytest.raises(ValueError, match="Azure OpenAI API key not found"):
            LLMExtractor(provider="azure_openai", endpoint="https://example.openai.azure.com")

    def test_azure_without_endpoint_raises(self, monkeypatch):
        fake_openai = types.SimpleNamespace(AzureOpenAI=lambda **kw: object())
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        with pytest.raises(ValueError, match="Azure OpenAI endpoint required"):
            LLMExtractor(provider="azure_openai", api_key="test-key")

    def test_azure_missing_package_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(ImportError, match="openai package not installed"):
            LLMExtractor(
                provider="azure_openai",
                api_key="test-key",
                endpoint="https://example.openai.azure.com",
            )

    def test_azure_success_builds_client_from_fake_package(self, monkeypatch):
        created = {}

        class FakeAzureClient:
            def __init__(self, **kwargs):
                created.update(kwargs)

        fake_openai = types.SimpleNamespace(AzureOpenAI=FakeAzureClient)
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        extractor = LLMExtractor(
            provider="azure_openai", api_key="test-key", endpoint="https://example.openai.azure.com"
        )

        assert isinstance(extractor._client, FakeAzureClient)
        assert created["api_key"] == "test-key"
        assert created["azure_endpoint"] == "https://example.openai.azure.com"
        assert extractor._call_fn == extractor._call_azure


# ---------------------------------------------------------------------------
# llm_extractor.py: the provider call methods, in isolation
# ---------------------------------------------------------------------------


class FakeChatCompletions:
    def __init__(self, content):
        self._content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = types.SimpleNamespace(content=self._content)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])


class FakeChatClient:
    def __init__(self, content):
        self.chat = types.SimpleNamespace(completions=FakeChatCompletions(content))


class TestLLMExtractorCallMethods:
    def _extractor(self):
        e = LLMExtractor.__new__(LLMExtractor)
        e.model = "test-model"
        e.temperature = 0.1
        e.max_tokens = 50
        return e

    def test_call_openai_returns_message_content(self):
        extractor = self._extractor()
        extractor._client = FakeChatClient("hello from openai")
        assert extractor._call_openai("prompt") == "hello from openai"
        call = extractor._client.chat.completions.calls[0]
        assert call["model"] == "test-model"
        assert call["messages"][-1] == {"role": "user", "content": "prompt"}

    def test_call_azure_returns_message_content(self):
        extractor = self._extractor()
        extractor._client = FakeChatClient("hello from azure")
        assert extractor._call_azure("prompt") == "hello from azure"

    def test_call_ollama_returns_response_field(self):
        extractor = self._extractor()
        calls = []

        def fake_generate(**kwargs):
            calls.append(kwargs)
            return {"response": "hello from ollama"}

        extractor._client = types.SimpleNamespace(generate=fake_generate)
        assert extractor._call_ollama("prompt") == "hello from ollama"
        assert calls[0]["model"] == "test-model"
        assert calls[0]["prompt"] == "prompt"

    def test_call_ollama_http_posts_and_parses_json(self, monkeypatch):
        """Against a real HTTP server on the loopback address, so the bytes on the wire are the ones Ollama would see."""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        monkeypatch.setitem(sys.modules, "requests", None)
        posts = []

        class Ollama(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                posts.append((self.path, self.headers["Content-Type"], json.loads(body)))
                reply = json.dumps({"response": "hello over http"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Ollama)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            extractor = self._extractor()
            extractor._endpoint = f"http://127.0.0.1:{server.server_port}"
            assert extractor._call_ollama_http("prompt") == "hello over http"
        finally:
            server.shutdown()
            server.server_close()
        path, kind, payload = posts[0]
        assert path == "/api/generate" and kind == "application/json"
        assert payload["model"] == "test-model" and payload["prompt"] == "prompt" and payload["stream"] is False

    def test_call_bedrock_asks_the_way_every_model_takes_and_reads_the_text(self):
        extractor = self._extractor()
        asked = []

        def fake_converse(**kwargs):
            asked.append(kwargs)
            return {"output": {"message": {"role": "assistant", "content": [{"text": "hello "}, {"text": "from bedrock"}]}}}

        extractor._client = types.SimpleNamespace(converse=fake_converse)
        assert extractor._call_bedrock("prompt") == "hello from bedrock"
        assert asked[0]["modelId"] == extractor.model and asked[0]["messages"] == [{"role": "user", "content": [{"text": "prompt"}]}]
        assert set(asked[0]["inferenceConfig"]) == {"maxTokens", "temperature"}

    def test_call_bedrock_defaults_to_empty_string_when_missing(self):
        extractor = self._extractor()
        extractor._client = types.SimpleNamespace(converse=lambda **kw: {})
        assert extractor._call_bedrock("prompt") == ""


# ---------------------------------------------------------------------------
# llm_extractor.py: retry logic
# ---------------------------------------------------------------------------


class TestLLMExtractorRetry:
    def _extractor(self, max_retries=3, retry_delay=0.0):
        e = LLMExtractor.__new__(LLMExtractor)
        e.max_retries = max_retries
        e.retry_delay = retry_delay
        return e

    def test_succeeds_on_first_try(self, monkeypatch):
        extractor = self._extractor()
        monkeypatch.setattr(llm_mod.time, "sleep", _raiser(AssertionError("should not sleep")))
        extractor._call_fn = lambda prompt: "ok"
        assert extractor._call_llm("prompt") == "ok"

    def test_succeeds_after_transient_failures(self, monkeypatch):
        extractor = self._extractor(max_retries=3, retry_delay=0.0)
        sleeps = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))
        attempts = {"n": 0}

        def flaky(prompt):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ValueError("transient")
            return "recovered"

        extractor._call_fn = flaky
        assert extractor._call_llm("prompt") == "recovered"
        assert attempts["n"] == 3
        assert len(sleeps) == 2

    def test_raises_last_error_after_exhausting_retries(self, monkeypatch):
        extractor = self._extractor(max_retries=2, retry_delay=0.0)
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
        extractor._call_fn = _raiser(ValueError("boom"))
        with pytest.raises(ValueError, match="boom"):
            extractor._call_llm("prompt")

    def test_zero_retries_raises_runtime_error(self):
        extractor = self._extractor(max_retries=0, retry_delay=0.0)
        extractor._call_fn = _raiser(ValueError("never called"))
        with pytest.raises(RuntimeError, match="no captured error"):
            extractor._call_llm("prompt")


# ---------------------------------------------------------------------------
# llm_extractor.py: _parse_json_response
# ---------------------------------------------------------------------------


class TestParseJsonResponse:
    def _extractor(self):
        return LLMExtractor.__new__(LLMExtractor)

    def test_direct_json(self):
        extractor = self._extractor()
        parsed = extractor._parse_json_response('{"entities": [], "relationships": []}')
        assert parsed == {"entities": [], "relationships": []}

    def test_json_fenced_block(self):
        extractor = self._extractor()
        response = 'Here:\n```json\n{"entities": [1], "relationships": []}\n```\n'
        assert extractor._parse_json_response(response) == {"entities": [1], "relationships": []}

    def test_plain_fenced_block(self):
        extractor = self._extractor()
        response = 'Here:\n```\n{"entities": [], "relationships": [2]}\n```\n'
        assert extractor._parse_json_response(response) == {"entities": [], "relationships": [2]}

    def test_braces_embedded_in_prose(self):
        extractor = self._extractor()
        response = 'Sure, here it is: {"entities": [], "relationships": []} Hope that helps!'
        assert extractor._parse_json_response(response) == {"entities": [], "relationships": []}

    def test_skips_an_invalid_match_before_a_valid_one(self):
        extractor = self._extractor()
        response = (
            "```json\nnot valid json\n```\n"
            'Actually:\n```json\n{"entities": [{"name": "Ada"}], "relationships": []}\n```\n'
        )
        parsed = extractor._parse_json_response(response)
        assert parsed == {"entities": [{"name": "Ada"}], "relationships": []}

    def test_no_json_anywhere_returns_empty_fallback(self):
        extractor = self._extractor()
        parsed = extractor._parse_json_response("I could not find anything useful to extract.")
        assert parsed == {"entities": [], "relationships": []}

    def test_empty_string_returns_empty_fallback(self):
        extractor = self._extractor()
        assert extractor._parse_json_response("") == {"entities": [], "relationships": []}


# ---------------------------------------------------------------------------
# llm_extractor.py: _convert_to_entities / _convert_to_relationships
# ---------------------------------------------------------------------------


class TestConvertHelpers:
    def _extractor(self):
        return LLMExtractor.__new__(LLMExtractor)

    def test_convert_to_entities_skips_blank_names(self):
        extractor = self._extractor()
        raw = [{"name": "  ", "type": "PERSON"}, {"name": "Ada", "type": "person"}]
        entities = extractor._convert_to_entities(raw, "u1")
        assert [e.name for e in entities] == ["Ada"]
        assert entities[0].type == EntityType.PERSON.value

    def test_convert_to_entities_falls_back_to_other_for_unknown_type(self):
        extractor = self._extractor()
        raw = [{"name": "Widget", "type": "GADGET"}]
        entities = extractor._convert_to_entities(raw, "u1")
        assert entities[0].type == EntityType.OTHER.value

    def test_convert_to_entities_defaults_missing_type_to_other(self):
        extractor = self._extractor()
        entities = extractor._convert_to_entities([{"name": "Widget"}], "u1")
        assert entities[0].type == EntityType.OTHER.value

    def test_convert_to_relationships_matches_case_insensitively(self):
        extractor = self._extractor()
        ada = Entity.create("Ada", EntityType.PERSON.value)
        acme = Entity.create("Acme", EntityType.ORGANIZATION.value)
        raw = [{"source": " ADA ", "target": "acme", "type": "works_for", "strength": 0.7}]
        rels = extractor._convert_to_relationships(raw, [ada, acme], "u1")
        assert len(rels) == 1
        rel = rels[0]
        assert rel.source_id == ada.id
        assert rel.target_id == acme.id
        assert rel.type == RelationshipType.WORKS_FOR.value
        assert rel.confidence == Confidence.EXTRACTED
        assert rel.strength == 0.7

    def test_convert_to_relationships_skips_unmatched_entities(self):
        extractor = self._extractor()
        ada = Entity.create("Ada", EntityType.PERSON.value)
        raw = [{"source": "Ada", "target": "Nobody", "type": "RELATED_TO"}]
        assert extractor._convert_to_relationships(raw, [ada], "u1") == []

    def test_convert_to_relationships_falls_back_to_related_to(self):
        extractor = self._extractor()
        ada = Entity.create("Ada", EntityType.PERSON.value)
        acme = Entity.create("Acme", EntityType.ORGANIZATION.value)
        raw = [{"source": "Ada", "target": "Acme", "type": "NOT_A_REAL_TYPE"}]
        rel = extractor._convert_to_relationships(raw, [ada, acme], "u1")[0]
        assert rel.type == RelationshipType.RELATED_TO.value

    @pytest.mark.parametrize(
        "raw_strength,expected",
        [
            ("not-a-number", 0.5),
            (5.0, 1.0),
            (-3.0, 0.0),
            (0.42, 0.42),
        ],
    )
    def test_convert_to_relationships_normalizes_strength(self, raw_strength, expected):
        extractor = self._extractor()
        ada = Entity.create("Ada", EntityType.PERSON.value)
        acme = Entity.create("Acme", EntityType.ORGANIZATION.value)
        raw = [{"source": "Ada", "target": "Acme", "type": "RELATED_TO", "strength": raw_strength}]
        rel = extractor._convert_to_relationships(raw, [ada, acme], "u1")[0]
        assert rel.strength == pytest.approx(expected)


# ---------------------------------------------------------------------------
# llm_extractor.py: extract_single / extract / extract_batch
# ---------------------------------------------------------------------------


def _make_llm_extractor(
    call_fn, max_retries=1, retry_delay=0.0, provider="test", model="test-model"
):
    extractor = LLMExtractor.__new__(LLMExtractor)
    extractor.provider = provider
    extractor.model = model
    extractor.api_key = None
    extractor.endpoint = None
    extractor.temperature = 0.0
    extractor.max_tokens = 128
    extractor.max_retries = max_retries
    extractor.retry_delay = retry_delay
    extractor._client = None
    extractor._call_fn = call_fn
    return extractor


CANNED_RESPONSE = json.dumps(
    {
        "entities": [
            {"name": "Ada Lovelace", "type": "PERSON", "description": "Mathematician"},
            {"name": "Analytical Engine", "type": "TECHNOLOGY", "description": "A machine"},
        ],
        "relationships": [
            {
                "source": "Ada Lovelace",
                "target": "Analytical Engine",
                "type": "CREATED_BY",
                "description": "designed programs for",
                "strength": 0.9,
            }
        ],
    }
)


class TestLLMExtractorExtractSingle:
    def test_happy_path(self):
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        result = extractor.extract_single("some text", doc_id="doc1")
        assert result.source_units == ["doc1_0"]
        assert result.entity_count == 2
        assert result.relationship_count == 1
        assert result.metadata == {"extractor": "llm", "provider": "test", "model": "test-model"}

    def test_callable_raises_is_caught_and_reported_in_metadata(self):
        extractor = _make_llm_extractor(_raiser(RuntimeError("provider is down")))
        result = extractor.extract_single("some text", doc_id="doc1")
        assert result.entities == []
        assert result.relationships == []
        assert result.source_units == ["doc1_0"]
        assert result.metadata == {"extractor": "llm", "error": "provider is down"}

    def test_malformed_output_yields_empty_result_without_an_error(self):
        extractor = _make_llm_extractor(lambda prompt: "not json and no braces at all")
        result = extractor.extract_single("some text", doc_id="doc1")
        assert result.entities == []
        assert result.relationships == []
        assert result.metadata == {"extractor": "llm", "provider": "test", "model": "test-model"}

    def test_empty_output_yields_empty_result(self):
        extractor = _make_llm_extractor(lambda prompt: "")
        result = extractor.extract_single("some text")
        assert result.entities == []
        assert result.relationships == []

    def test_partial_json_missing_relationships_key(self):
        response = json.dumps({"entities": [{"name": "Solo", "type": "PERSON"}]})
        extractor = _make_llm_extractor(lambda prompt: response)
        result = extractor.extract_single("some text")
        assert result.entity_count == 1
        assert result.relationships == []


class TestLLMExtractorExtract:
    def test_empty_list_returns_bare_result_without_metadata(self):
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        result = extractor.extract([])
        assert result.entities == []
        assert result.metadata == {}

    def test_multiple_units_merge_and_set_final_metadata(self):
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        units = [make_unit("u1", "text one"), make_unit("u2", "text two")]
        result = extractor.extract(units)
        assert result.metadata == {"extractor": "llm", "provider": "test", "model": "test-model"}
        assert result.entity_count == 2  # the two units describe the same two entities, merged

    def test_one_failing_unit_is_recorded_but_does_not_stop_the_rest(self):
        def flaky(prompt):
            if "bad" in prompt:
                raise RuntimeError("boom")
            return CANNED_RESPONSE

        extractor = _make_llm_extractor(flaky)
        units = [make_unit("u1", "bad text"), make_unit("u2", "good text")]

        result = extractor.extract(units)

        assert "u1" in result.source_units
        assert result.entity_count == 2


class TestLLMExtractorExtractBatch:
    def test_empty_list(self):
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        assert extractor.extract_batch([]).entities == []

    def test_parallel_extraction_merges_results(self):
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        units = [make_unit(f"u{i}", f"text {i}") for i in range(4)]
        result = extractor.extract_batch(units, max_workers=2)
        assert result.entity_count == 2
        assert result.metadata == {"extractor": "llm", "provider": "test", "model": "test-model"}

    def test_a_failing_unit_is_recorded_by_id(self):
        def flaky(prompt):
            if "fails" in prompt:
                raise RuntimeError("boom")
            return CANNED_RESPONSE

        extractor = _make_llm_extractor(flaky)
        units = [make_unit("good", "text"), make_unit("bad", "this one fails")]

        result = extractor.extract_batch(units)

        assert "bad" in result.source_units

    def test_batch_size_bounds_concurrent_submissions(self, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor as RealExecutor

        submit_counts = []

        class RecordingExecutor:
            def __init__(self, max_workers=None):
                self._real = RealExecutor(max_workers=max_workers)
                self._count = 0

            def submit(self, fn, *args, **kwargs):
                self._count += 1
                submit_counts.append(self._count)
                return self._real.submit(fn, *args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._real.__exit__(exc_type, exc, tb)

        monkeypatch.setattr(llm_mod, "ThreadPoolExecutor", RecordingExecutor)
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        units = [make_unit(f"u{i}", f"text {i}") for i in range(4)]

        extractor.extract_batch(units, batch_size=1, max_workers=4)

        assert max(submit_counts) <= 1


class TestExtractSingleUnit:
    def test_returns_result_with_no_metadata(self):
        extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)
        unit = make_unit("u1", "text")
        result = extractor._extract_single_unit(unit)
        assert result.source_units == ["u1"]
        assert result.metadata == {}
        assert result.entity_count == 2


class TestCreateLLMExtractor:
    def test_without_config_uses_openai_default_and_needs_a_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        fake_openai = types.SimpleNamespace(OpenAI=lambda **kw: object())
        monkeypatch.setitem(sys.modules, "openai", fake_openai)
        with pytest.raises(ValueError, match="OpenAI API key not found"):
            create_llm_extractor()

    def test_with_config_builds_a_configured_extractor(self):
        config = GraphRAGConfig(llm_provider=LLMProvider.OLLAMA, llm_model="phi3")
        extractor = create_llm_extractor(config)
        assert isinstance(extractor, LLMExtractor)
        assert extractor.provider == "ollama"
        assert extractor.model == "phi3"


# ---------------------------------------------------------------------------
# hybrid_extractor.py: loaders
# ---------------------------------------------------------------------------


class TestHybridExtractorLoaders:
    def _extractor(self, **overrides):
        e = HybridExtractor.__new__(HybridExtractor)
        e.spacy_ner_model = overrides.get("spacy_ner_model", "xx_ent_wiki_sm")
        e.spacy_sent_model = overrides.get("spacy_sent_model", "xx_sent_ud_sm")
        e.use_rebel = overrides.get("use_rebel", True)
        e._nlp_ner = overrides.get("nlp_ner", None)
        e._nlp_sent = overrides.get("nlp_sent", None)
        e._rebel = overrides.get("rebel", None)
        return e

    def test_ensure_spacy_loaded_is_a_noop_once_loaded(self):
        sentinel = object()
        extractor = self._extractor(nlp_ner=sentinel)
        extractor._ensure_spacy_loaded()
        assert extractor._nlp_ner is sentinel

    def test_ensure_spacy_loaded_missing_package_logs_and_reraises(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "spacy", None)
        extractor = self._extractor()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ImportError):
                extractor._ensure_spacy_loaded()
        assert "spaCy is not installed" in caplog.text

    def test_ensure_spacy_loaded_success_loads_ner_and_sentence_models(self, monkeypatch):
        fake_ner = object()
        fake_sent = object()

        def fake_load(name):
            return fake_ner if name == "xx_ent_wiki_sm" else fake_sent

        fake_spacy = make_fake_spacy_module(load_fn=fake_load)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        extractor = self._extractor()

        extractor._ensure_spacy_loaded()

        assert extractor._nlp_ner is fake_ner
        assert extractor._nlp_sent is fake_sent

    def test_ensure_spacy_loaded_missing_ner_model_without_auto_download_raises(self, monkeypatch):
        from vectrixdb.exceptions import ModelDownloadError

        fake_spacy = make_fake_spacy_module(load_fn=_raiser(OSError("missing")))
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.delenv("VECTRIXDB_AUTO_DOWNLOAD", raising=False)
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        extractor = self._extractor()

        with pytest.raises(ModelDownloadError):
            extractor._ensure_spacy_loaded()

    def test_ensure_spacy_loaded_missing_ner_model_downloads_when_allowed(self, monkeypatch):
        calls = {"n": 0}
        fake_ner = object()

        def fake_load(name):
            calls["n"] += 1
            if name == "xx_ent_wiki_sm" and calls["n"] == 1:
                raise OSError("missing")
            return fake_ner

        fake_spacy = make_fake_spacy_module(load_fn=fake_load)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")
        monkeypatch.delenv("VECTRIXDB_OFFLINE", raising=False)
        extractor = self._extractor()

        extractor._ensure_spacy_loaded()

        assert extractor._nlp_ner is fake_ner
        assert "xx_ent_wiki_sm" in fake_spacy.download_calls

    def test_ensure_spacy_loaded_missing_sentence_model_is_optional(self, monkeypatch):
        fake_ner = object()

        def fake_load(name):
            if name == "xx_ent_wiki_sm":
                return fake_ner
            raise OSError("no sentence model")

        fake_spacy = make_fake_spacy_module(load_fn=fake_load)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.delenv("VECTRIXDB_AUTO_DOWNLOAD", raising=False)
        extractor = self._extractor()

        extractor._ensure_spacy_loaded()

        assert extractor._nlp_ner is fake_ner
        assert extractor._nlp_sent is None

    def test_ensure_spacy_loaded_missing_sentence_model_downloads_when_allowed(self, monkeypatch):
        fake_ner = object()
        fake_sent = object()
        sent_calls = {"n": 0}

        def fake_load(name):
            if name == "xx_ent_wiki_sm":
                return fake_ner
            sent_calls["n"] += 1
            if sent_calls["n"] == 1:
                raise OSError("no sentence model yet")
            return fake_sent

        fake_spacy = make_fake_spacy_module(load_fn=fake_load)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")
        extractor = self._extractor()

        extractor._ensure_spacy_loaded()

        assert extractor._nlp_ner is fake_ner
        assert extractor._nlp_sent is fake_sent
        assert "xx_sent_ud_sm" in fake_spacy.download_calls

    def test_ensure_spacy_loaded_sentence_model_download_failure_is_swallowed(self, monkeypatch):
        fake_ner = object()

        def fake_load(name):
            if name == "xx_ent_wiki_sm":
                return fake_ner
            raise OSError("no sentence model")

        def fake_download(name):
            raise RuntimeError("network is somehow on fire")

        fake_spacy = make_fake_spacy_module(load_fn=fake_load, download_fn=fake_download)
        monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
        monkeypatch.setenv("VECTRIXDB_AUTO_DOWNLOAD", "1")
        extractor = self._extractor()

        extractor._ensure_spacy_loaded()

        assert extractor._nlp_ner is fake_ner
        assert extractor._nlp_sent is None

    def test_ensure_rebel_loaded_noop_when_disabled(self):
        extractor = self._extractor(use_rebel=False)
        extractor._ensure_rebel_loaded()
        assert extractor._rebel is None

    def test_ensure_rebel_loaded_noop_when_already_loaded(self):
        sentinel = object()
        extractor = self._extractor(rebel=sentinel)
        extractor._ensure_rebel_loaded()
        assert extractor._rebel is sentinel

    def test_ensure_rebel_loaded_disables_when_model_not_installed(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "vectrixdb.models.embedded.is_models_installed", lambda model_type: False
        )
        extractor = self._extractor(use_rebel=True)

        logger_name = "vectrixdb.core.graphrag.extractor.hybrid_extractor"
        with caplog.at_level("INFO", logger=logger_name):
            extractor._ensure_rebel_loaded()

        assert extractor.use_rebel is False
        assert extractor._rebel is None
        assert "not installed" in caplog.text

    def test_ensure_rebel_loaded_builds_model_when_installed(self, monkeypatch):
        fake_instance = object()
        monkeypatch.setattr(
            "vectrixdb.models.embedded.is_models_installed", lambda model_type: True
        )
        monkeypatch.setattr("vectrixdb.models.embedded.REBELExtractor", lambda: fake_instance)
        extractor = self._extractor(use_rebel=True)

        extractor._ensure_rebel_loaded()

        assert extractor._rebel is fake_instance


# ---------------------------------------------------------------------------
# hybrid_extractor.py: entity and relation extraction
# ---------------------------------------------------------------------------


class TestHybridExtractorEntities:
    def _extractor(self):
        e = HybridExtractor.__new__(HybridExtractor)
        e.use_rebel = True
        e._rebel = None
        return e

    def test_extract_entities_spacy_dedupes_and_filters(self):
        doc = FakeDoc(
            ents=[
                FakeEnt("Albert Einstein", "PERSON", 0),
                FakeEnt("albert einstein", "PERSON", 40),  # duplicate, case-insensitive
                FakeEnt("X", "PERSON", 60),  # too short
                FakeEnt("Germany", "GPE", 70),
                FakeEnt("A Play", "MISC", 90),
            ]
        )
        extractor = self._extractor()
        extractor._nlp_ner = FakeNLP(doc_factory=lambda text: doc)

        entities = extractor._extract_entities_spacy("ignored", "u1")

        by_name = {e.name: e for e in entities}
        assert list(by_name) == ["Albert Einstein", "Germany", "A Play"]
        assert by_name["Albert Einstein"].type == EntityType.PERSON.value
        assert by_name["Germany"].type == EntityType.LOCATION.value
        assert by_name["A Play"].type == EntityType.OTHER.value  # MISC
        assert by_name["Germany"].attributes["extractor"] == "spacy"
        assert by_name["Germany"].attributes["spacy_label"] == "GPE"


class TestHybridExtractorRelations:
    def _extractor(self, use_rebel=True, rebel=None):
        e = HybridExtractor.__new__(HybridExtractor)
        e.use_rebel = use_rebel
        e._rebel = rebel
        return e

    def test_disabled_returns_no_relationships(self):
        extractor = self._extractor(use_rebel=False)
        assert extractor._extract_relations_rebel("text", "u1", []) == []

    def test_unavailable_after_ensure_returns_no_relationships(self, monkeypatch):
        extractor = self._extractor(use_rebel=True, rebel=None)
        monkeypatch.setattr(HybridExtractor, "_ensure_rebel_loaded", lambda self: None)
        assert extractor._extract_relations_rebel("text", "u1", []) == []

    def test_reuses_existing_entities_by_name(self):
        einstein = Entity.create("Albert Einstein", EntityType.PERSON.value, source_unit_id="u1")
        germany = Entity.create("Germany", EntityType.LOCATION.value, source_unit_id="u1")
        triplet = FakeTriplet("Albert Einstein", "PER", "country of birth", "Germany", "GPE")
        extractor = self._extractor(rebel=FakeRebel([triplet]))

        entities = [einstein, germany]
        rels = extractor._extract_relations_rebel("text", "u1", entities)

        assert len(entities) == 2  # no new entities were created
        assert len(rels) == 1
        rel = rels[0]
        assert rel.source_id == einstein.id
        assert rel.target_id == germany.id
        assert rel.type == RelationshipType.LOCATED_IN.value
        assert rel.confidence == Confidence.EXTRACTED
        assert rel.attributes == {"extractor": "rebel", "original_relation": "country of birth"}

    def test_creates_entities_for_unmatched_triplet_endpoints(self):
        triplet = FakeTriplet("Marie Curie", "PER", "founded_by", "Radium Institute", "ORG")
        extractor = self._extractor(rebel=FakeRebel([triplet]))

        entities = []
        rels = extractor._extract_relations_rebel("text", "u1", entities)

        assert len(entities) == 2  # mutated in place
        assert {e.name for e in entities} == {"Marie Curie", "Radium Institute"}
        assert all(e.attributes["extractor"] == "rebel" for e in entities)
        assert rels[0].type == RelationshipType.CREATED_BY.value

    @pytest.mark.parametrize(
        "raw_type,expected",
        [
            ("person", EntityType.PERSON),
            ("org", EntityType.ORGANIZATION),
            ("city", EntityType.LOCATION),
            ("product", EntityType.PRODUCT),
            ("something-else", EntityType.CONCEPT),
        ],
    )
    def test_infer_entity_type(self, raw_type, expected):
        extractor = self._extractor()
        assert extractor._infer_entity_type(raw_type) == expected

    @pytest.mark.parametrize(
        "relation,expected",
        [
            ("works for", RelationshipType.WORKS_FOR),
            ("part of", RelationshipType.PART_OF),
            ("uses", RelationshipType.USES),
            ("some unknown relation", RelationshipType.RELATED_TO),
        ],
    )
    def test_normalize_relation(self, relation, expected):
        extractor = self._extractor()
        assert extractor._normalize_relation(relation) == expected


class TestHybridExtractorPipeline:
    def _extractor(
        self, ner_doc_factory, rebel_triplets=(), use_rebel=True, spacy_model="xx_ent_wiki_sm"
    ):
        e = HybridExtractor.__new__(HybridExtractor)
        e.spacy_ner_model = spacy_model
        e.use_rebel = use_rebel
        e._nlp_ner = FakeNLP(doc_factory=ner_doc_factory)
        e._rebel = FakeRebel(rebel_triplets) if use_rebel else None
        return e

    def test_extract_single_builds_metadata_and_dedupes(self):
        doc = FakeDoc(ents=[FakeEnt("Albert Einstein", "PERSON", 0), FakeEnt("Germany", "GPE", 30)])
        triplet = FakeTriplet("Albert Einstein", "PER", "country of birth", "Germany", "GPE")
        extractor = self._extractor(lambda text: doc, rebel_triplets=[triplet])

        result = extractor.extract_single("Albert Einstein was born in Germany.")

        assert result.entity_count == 2
        assert result.relationship_count == 1
        assert result.metadata["extractor"] == "hybrid"
        assert result.metadata["rebel_model"] == "mrebel-base-int8"
        assert result.metadata["entities_from_spacy"] == 2
        assert result.metadata["entities_from_rebel"] == 0

    def test_extract_single_merges_duplicate_names_from_its_own_entity_list(self, monkeypatch):
        # _extract_entities_spacy and _extract_relations_rebel each dedupe their
        # own output, so drive extract_single's own entity_dict merge branch
        # directly with a stub that hands back a same-name duplicate.
        first = Entity.create("Germany", EntityType.LOCATION.value, source_unit_id="u1")
        second = Entity.create("germany", EntityType.LOCATION.value, source_unit_id="u2")
        monkeypatch.setattr(
            HybridExtractor, "_extract_entities_spacy", lambda self, text, uid: [first, second]
        )
        extractor = self._extractor(lambda text: FakeDoc(), use_rebel=False)

        result = extractor.extract_single("ignored")

        assert result.entity_count == 1
        assert set(result.entities[0].source_units) == {"u1", "u2"}

    def test_extract_single_without_rebel_has_no_relationships(self):
        doc = FakeDoc(ents=[FakeEnt("Germany", "GPE", 0)])
        extractor = self._extractor(lambda text: doc, use_rebel=False)

        result = extractor.extract_single("Germany is a country.")

        assert result.relationship_count == 0
        assert result.metadata["rebel_model"] is None

    def test_extract_empty_list_returns_default_result(self):
        extractor = self._extractor(lambda text: FakeDoc())
        assert extractor.extract([]).entities == []

    def test_extract_merges_entities_with_the_same_name_across_units(self):
        doc1 = FakeDoc(ents=[FakeEnt("Germany", "GPE", 0)])
        doc2 = FakeDoc(ents=[FakeEnt("germany", "GPE", 0)])
        docs = iter([doc1, doc2])
        extractor = self._extractor(lambda text: next(docs), use_rebel=False)
        units = [make_unit("u1", "Germany one."), make_unit("u2", "germany two.")]

        result = extractor.extract(units)

        assert result.entity_count == 1
        assert set(result.entities[0].source_units) == {"u1", "u2"}
        assert result.metadata["text_units_processed"] == 2

    def test_extract_batch_delegates_to_extract(self, monkeypatch):
        extractor = self._extractor(lambda text: FakeDoc())
        called = {}
        monkeypatch.setattr(
            extractor,
            "extract",
            lambda units: called.setdefault("units", units) or ExtractionResult(),
        )
        units = [make_unit("u1", "text")]
        extractor.extract_batch(units, batch_size=5)
        assert called["units"] == units


class TestCreateHybridExtractor:
    def test_returns_a_hybrid_extractor(self):
        extractor = hybrid_mod.create_hybrid_extractor()
        assert isinstance(extractor, HybridExtractor)
        assert extractor._nlp_ner is None
        assert extractor._rebel is None


# ---------------------------------------------------------------------------
# extractor/__init__.py: the create_extractor factory and HybridLLMExtractor
# ---------------------------------------------------------------------------


class TestCreateExtractorFactory:
    def test_default_is_spacy_rebel(self):
        extractor = extractor_pkg.create_extractor()
        assert isinstance(extractor, extractor_pkg.SpacyRebelExtractor)

    def test_rebel(self):
        extractor = extractor_pkg.create_extractor("rebel")
        assert isinstance(extractor, extractor_pkg.REBELExtractor)

    def test_nlp(self):
        extractor = extractor_pkg.create_extractor("nlp")
        assert isinstance(extractor, extractor_pkg.NLPExtractor)

    def test_hybrid_is_the_nlp_plus_llm_variant(self):
        extractor = extractor_pkg.create_extractor("hybrid")
        assert isinstance(extractor, extractor_pkg.HybridLLMExtractor)
        assert extractor._llm_extractor is None  # no api key, no ollama provider

    def test_llm_via_ollama_kwargs(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "ollama", None
        )  # force the plain HTTP fallback, deterministically
        extractor = extractor_pkg.create_extractor("llm", provider="ollama")
        assert isinstance(extractor, extractor_pkg.LLMExtractor)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown extractor type"):
            extractor_pkg.create_extractor("not-a-real-type")

    def test_config_overrides_the_extractor_type_argument(self):
        config = GraphRAGConfig()  # defaults to ExtractorType.REBEL
        extractor = extractor_pkg.create_extractor("nlp", config=config)
        assert isinstance(extractor, extractor_pkg.REBELExtractor)


class TestHybridLLMExtractor:
    def test_identify_important_units_selects_top_fraction_by_entity_count(self):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        extractor.importance_threshold = 0.5
        extractor.min_entities_for_llm = 2

        units = [make_unit(f"u{i}", "text", position=i) for i in range(4)]
        busy = Entity.create("A", EntityType.OTHER.value, source_unit_id="u0")
        busy2 = Entity.create("B", EntityType.OTHER.value, source_unit_id="u0")
        quiet = Entity.create("C", EntityType.OTHER.value, source_unit_id="u1")
        nlp_result = ExtractionResult(entities=[busy, busy2, quiet])

        important = extractor._identify_important_units(units, nlp_result)

        assert [u.id for u in important] == ["u0"]

    def test_extract_without_llm_available_tags_metadata(self):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        extractor.importance_threshold = 0.3
        extractor.min_entities_for_llm = 3
        extractor._llm_extractor = None

        regex_nlp = NLPExtractor.__new__(NLPExtractor)
        regex_nlp._nlp = None
        regex_nlp.extract_noun_phrases = True
        regex_nlp.min_entity_length = 2
        regex_nlp.max_entity_length = 100
        regex_nlp.model_name = "regex-fallback"
        extractor.nlp_extractor = regex_nlp

        units = [make_unit("u1", "Marie Curie worked in Paris.")]
        result = extractor.extract(units)

        assert result.metadata["extractor"] == "hybrid_nlp_only"

    def test_extract_empty_list(self):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        assert extractor.extract([]).entities == []

    def _extractor_with_stub_nlp(self, nlp_result):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        extractor.importance_threshold = 1.0
        extractor.min_entities_for_llm = 2

        class _StubNLP:
            def extract(self, text_units):
                return nlp_result

        extractor.nlp_extractor = _StubNLP()
        return extractor

    def test_extract_with_llm_but_no_important_units_stays_nlp_only(self):
        units = [make_unit("u1", "text"), make_unit("u2", "text")]
        # One entity total, spread thin: no unit reaches min_entities_for_llm.
        nlp_result = ExtractionResult(
            entities=[Entity.create("Solo", EntityType.OTHER.value, source_unit_id="u1")]
        )
        extractor = self._extractor_with_stub_nlp(nlp_result)
        extractor._llm_extractor = object()  # truthy; must not be used

        result = extractor.extract(units)

        assert result.metadata["extractor"] == "hybrid_nlp_only"

    def test_extract_llm_stage_exception_falls_back_to_nlp_only(self):
        units = [make_unit("u1", "text"), make_unit("u2", "text")]
        busy = [
            Entity.create("A", EntityType.OTHER.value, source_unit_id="u1"),
            Entity.create("B", EntityType.OTHER.value, source_unit_id="u1"),
        ]
        nlp_result = ExtractionResult(entities=busy)
        extractor = self._extractor_with_stub_nlp(nlp_result)

        class _RaisingLLM:
            def extract(self, text_units):
                raise RuntimeError("provider down")

        extractor._llm_extractor = _RaisingLLM()

        result = extractor.extract(units)

        assert result.metadata["extractor"] == "hybrid_nlp_only"

    def test_extract_full_success_merges_llm_into_nlp(self):
        units = [make_unit("u1", "text"), make_unit("u2", "text")]
        busy = [
            Entity.create("A", EntityType.OTHER.value, source_unit_id="u1"),
            Entity.create("B", EntityType.OTHER.value, source_unit_id="u1"),
        ]
        nlp_result = ExtractionResult(entities=busy)
        extractor = self._extractor_with_stub_nlp(nlp_result)

        llm_entity = Entity.create("C", EntityType.OTHER.value, source_unit_id="u1")

        class _SucceedingLLM:
            def extract(self, text_units):
                self.seen_units = text_units
                return ExtractionResult(entities=[llm_entity])

        stub_llm = _SucceedingLLM()
        extractor._llm_extractor = stub_llm

        result = extractor.extract(units)

        assert result.metadata == {"extractor": "hybrid", "nlp_units": 2, "llm_units": 1}
        assert {e.name for e in result.entities} == {"A", "B", "C"}
        assert stub_llm.seen_units == [units[0]]  # only the important (busy) unit

    def test_extract_single_enhances_with_llm_when_available(self):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        extractor.min_entities_for_llm = 1

        regex_nlp = NLPExtractor.__new__(NLPExtractor)
        regex_nlp._nlp = None
        regex_nlp.extract_noun_phrases = False
        regex_nlp.min_entity_length = 2
        regex_nlp.max_entity_length = 100
        regex_nlp.model_name = "regex-fallback"
        extractor.nlp_extractor = regex_nlp

        extractor._llm_extractor = _make_llm_extractor(lambda prompt: CANNED_RESPONSE)

        result = extractor.extract_single("Marie Curie worked in Paris.")

        # The LLM's canned entities made it into the merged result, proving the
        # LLM branch (not a plain NLP-only return) is what ran.
        assert "Ada Lovelace" in {e.name for e in result.entities}

    def test_extract_single_falls_back_to_nlp_when_llm_raises(self):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        extractor.min_entities_for_llm = 1

        regex_nlp = NLPExtractor.__new__(NLPExtractor)
        regex_nlp._nlp = None
        regex_nlp.extract_noun_phrases = False
        regex_nlp.min_entity_length = 2
        regex_nlp.max_entity_length = 100
        regex_nlp.model_name = "regex-fallback"
        extractor.nlp_extractor = regex_nlp

        # A real LLMExtractor.extract_single swallows its own exceptions and
        # returns a normal (empty) result, so it never reaches this method's
        # except clause. Use a stub whose extract_single actually raises, to
        # drive that except branch directly.
        class _RaisingLLM:
            def extract_single(self, text, doc_id="default"):
                raise RuntimeError("down")

        extractor._llm_extractor = _RaisingLLM()

        result = extractor.extract_single("Marie Curie worked in Paris.")

        assert result.metadata["extractor"] == "nlp"

    def test_extract_batch_delegates_to_extract(self, monkeypatch):
        extractor = extractor_pkg.HybridLLMExtractor.__new__(extractor_pkg.HybridLLMExtractor)
        called = {}
        monkeypatch.setattr(
            extractor,
            "extract",
            lambda units: called.setdefault("units", units) or ExtractionResult(),
        )
        units = [make_unit("u1", "text")]
        extractor.extract_batch(units)
        assert called["units"] == units

    def test_init_skips_llm_when_no_key_and_not_ollama(self):
        extractor = extractor_pkg.HybridLLMExtractor(config=GraphRAGConfig())
        assert extractor._llm_extractor is None

    def test_init_builds_llm_for_ollama_provider(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ollama", None)  # deterministic plain HTTP fallback
        config = GraphRAGConfig(llm_provider=LLMProvider.OLLAMA)
        extractor = extractor_pkg.HybridLLMExtractor(config=config)
        assert isinstance(extractor._llm_extractor, LLMExtractor)

    def test_init_swallows_llm_construction_failure(self, monkeypatch):
        monkeypatch.setattr(
            extractor_pkg,
            "LLMExtractor",
            _raiser(RuntimeError("no client available")),
        )
        config = GraphRAGConfig(llm_provider=LLMProvider.OLLAMA)
        extractor = extractor_pkg.HybridLLMExtractor(config=config)
        assert extractor._llm_extractor is None
