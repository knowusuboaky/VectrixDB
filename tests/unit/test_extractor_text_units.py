"""The multi-unit extractors read the field a TextUnit actually has.

``TextUnit`` carries its text on ``.text``. The REBEL extractor and the
hybrid extractor both read ``.content`` in their multi-unit paths, so
extracting a graph over more than one chunk raised AttributeError on the
first unit. No model is loaded here: the model loading and the per-unit work
are replaced, which leaves the attribute access under test and nothing else.
"""

from __future__ import annotations

from vectrixdb.core.graphrag.chunker import TextUnit


def _units():
    return [
        TextUnit(
            id="u1",
            text="Ada Lovelace wrote the first algorithm.",
            doc_id="d1",
            position=0,
            token_count=6,
        ),
        TextUnit(
            id="u2",
            text="Charles Babbage designed the Analytical Engine.",
            doc_id="d1",
            position=1,
            token_count=6,
        ),
    ]


def test_rebel_extractor_reads_text(monkeypatch):
    from vectrixdb.core.graphrag.extractor import rebel_extractor as mod

    seen = []

    class _Inner:
        def extract(self, text):
            seen.append(text)
            return []

    extractor = mod.REBELExtractor.__new__(mod.REBELExtractor)
    monkeypatch.setattr(mod.REBELExtractor, "_ensure_model_loaded", lambda self: None)
    extractor._extractor = _Inner()

    extractor.extract(_units())

    assert seen == [u.text for u in _units()]


def test_hybrid_extractor_reads_text(monkeypatch):
    from vectrixdb.core.graphrag.extractor import hybrid_extractor as mod

    seen = []

    def _entities(self, text, unit_id):
        seen.append(text)
        return []

    extractor = mod.HybridExtractor.__new__(mod.HybridExtractor)
    # __new__ skips __init__, so the two fields the result metadata reads are
    # set by hand. Nothing loads spaCy or mREBEL.
    extractor.spacy_ner_model = "stub"
    extractor.use_rebel = False
    monkeypatch.setattr(mod.HybridExtractor, "_extract_entities_spacy", _entities)
    monkeypatch.setattr(
        mod.HybridExtractor,
        "_extract_relations_rebel",
        lambda self, text, unit_id, entities: [],
    )

    extractor.extract(_units())

    assert seen == [u.text for u in _units()]


def test_text_unit_has_no_content_field():
    """The attribute the two extractors used to read does not exist, which is
    why reading it was a crash and not a wrong answer."""
    unit = TextUnit(id="u", text="x", doc_id="d", position=0, token_count=1)
    assert not hasattr(unit, "content")
    assert unit.text == "x"
