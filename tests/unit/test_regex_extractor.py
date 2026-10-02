"""The regex fallback has to see something in ordinary lowercase prose.

Without spaCy the extractor matched capitalised phrases and nothing else, so
chat logs, tickets and notes produced no entities and graph mode silently had
no graph. It still is a heuristic, and it says so once at load time.
"""

import logging

import pytest

from vectrixdb.core.graphrag.extractor import nlp_extractor
from vectrixdb.core.graphrag.extractor.base import EntityType
from vectrixdb.core.graphrag.extractor.nlp_extractor import NLPExtractor


@pytest.fixture
def regex_only() -> NLPExtractor:
    extractor = NLPExtractor()
    extractor._nlp = None  # force the fallback even where spaCy is installed
    return extractor


def _names(result) -> dict:
    return {e.name.lower(): e.type for e in result.entities}


class TestSources:
    def test_capitalised_phrases_still_work(self, regex_only):
        names = _names(regex_only.extract_single("Marie Curie worked in Paris."))
        assert "marie curie" in names and "paris" in names

    def test_acronyms(self, regex_only):
        names = _names(regex_only.extract_single("export the report as a PDF or CSV"))
        assert "pdf" in names and "csv" in names

    def test_quoted_terms(self, regex_only):
        names = _names(regex_only.extract_single('the user picked "dark mode" in settings'))
        assert names.get("dark mode") == EntityType.CONCEPT.value

    def test_repeated_lowercase_phrases(self, regex_only):
        text = (
            "the export button greys out when a filter is active. "
            "clearing the filter brings the export button back."
        )
        names = _names(regex_only.extract_single(text))
        assert names.get("export button") == EntityType.CONCEPT.value

    def test_a_phrase_seen_once_is_not_an_entity(self, regex_only):
        names = _names(regex_only.extract_single("the export button greys out sometimes"))
        assert "export button" not in names

    def test_phrases_do_not_start_or_end_on_stopwords(self, regex_only):
        text = "click on the button. click on the button again."
        names = _names(regex_only.extract_single(text))
        assert "on the" not in names and "the button" not in names

    def test_phrases_do_not_cross_punctuation(self, regex_only):
        text = "reset password, then login. reset password, then login."
        names = _names(regex_only.extract_single(text))
        assert "password then" not in names
        assert "reset password" in names

    def test_a_longer_phrase_absorbs_its_parts(self, regex_only):
        text = "annual billing plan renews yearly. annual billing plan is default."
        names = _names(regex_only.extract_single(text))
        assert "annual billing plan" in names
        assert "billing plan" not in names and "annual billing" not in names


class TestAnnouncement:
    def test_fallback_is_warned_once_with_the_install_hint(self, monkeypatch, caplog):
        monkeypatch.setattr(nlp_extractor, "_FALLBACK_WARNED", False)
        monkeypatch.setitem(__import__("sys").modules, "spacy", None)  # ImportError
        caplog.set_level(logging.WARNING, logger="vectrixdb")

        NLPExtractor()
        NLPExtractor()

        warnings = [r for r in caplog.records if "spaCy is not installed" in r.getMessage()]
        assert len(warnings) == 1
        assert "vectrixdb[nlp]" in warnings[0].getMessage()
        assert "spacy download" in warnings[0].getMessage()

    def test_nothing_is_printed(self, monkeypatch, capsys):
        monkeypatch.setattr(nlp_extractor, "_FALLBACK_WARNED", False)
        monkeypatch.setitem(__import__("sys").modules, "spacy", None)
        NLPExtractor()
        assert capsys.readouterr().out == ""
