"""Every chunk has a name an answer can cite, and an answer's brackets are
checked against the sources it was given.

The check is the point. A model handed three sources will cite a fourth it
made up, and it will look like the other three. A bracket that names nothing
the model was given becomes text and is listed as rejected; it is never
shown as a citation.
"""

from __future__ import annotations

import pytest

from vectrixdb.citations import (
    CITATION_INSTRUCTION,
    Cited,
    citation_for,
    citation_of,
    parse_citations,
    validate_answer,
)
from vectrixdb.ingest import LoadedDocument

GUIDE = """# Install

Install vectrixdb with pip and you are done. The English model ships in the wheel.

## Offline

Nothing downloads unless you ask by name. The offline switch makes that a hard rule.

# Search

Call search with a query string. Results carry text, score and metadata.
"""


class TestCitationStrings:
    def test_paged_source(self):
        assert citation_for("reports/q3.pdf", "d1", page=3) == "q3.pdf#page=3"
        assert citation_for(r"C:\docs\q3.pdf", "d1", page=1) == "q3.pdf#page=1"

    def test_heading_when_no_page(self):
        assert citation_for("guide", "d1", heading="Offline") == "guide#Offline"
        assert citation_for("guide", "d1", heading="[Not] this") == "guide#Not this"

    def test_brackets_in_a_file_name_are_dropped(self):
        """``Invoice [final].pdf`` inside a citation's brackets ended it early,
        so no answer citing it could validate."""
        from vectrixdb.citations import readable_citation_for

        cite = citation_for("scans/Invoice [final].pdf", "d1", page=2)
        assert cite == "Invoice final.pdf#page=2"
        assert (
            readable_citation_for("Invoice [final].pdf", "d1", page=2) == "Invoice final.pdf, p. 2"
        )
        c = validate_answer(f"Due in 30 days [{cite}].", [cite])
        assert c.cited == [cite] and c.clean

    def test_page_wins_over_heading(self):
        assert citation_for("manual.docx", "d1", page=2, heading="Setup") == "manual.docx#page=2"

    def test_bare_name_and_fallback(self):
        assert citation_for("guide", "d1") == "guide"
        assert citation_for(None, "doc-42") == "doc-42"
        assert citation_for("   ", "doc-42") == "doc-42"

    def test_a_label_with_spaces_is_not_a_path(self):
        assert citation_for("HR handbook 2026", "d1") == "HR handbook 2026"

    def test_citation_of_prefers_the_stamp(self):
        assert (
            citation_of({"_vx_citation": "x.pdf#page=2", "source": "y.pdf", "page": 9}, "id")
            == "x.pdf#page=2"
        )
        assert citation_of({"filename": "y.pdf", "page": 9}, "id") == "y.pdf#page=9"
        assert citation_of({}, "chunk-7") == "chunk-7"


class TestParsingAndValidation:
    SOURCES = ["guide#Offline", "reports/q3.pdf#page=2", "q3.pdf#page=3"]

    def test_parse_keeps_order_and_duplicates(self):
        assert parse_citations("a [x] b [y] c [x]") == ["x", "y", "x"]

    def test_valid_brackets_stay_and_are_listed_once(self):
        c = validate_answer(
            "Offline is a hard rule [guide#Offline]. Again [guide#Offline].", self.SOURCES
        )
        assert c.text == "Offline is a hard rule [guide#Offline]. Again [guide#Offline]."
        assert c.cited == ["guide#Offline"] and c.rejected == [] and c.clean

    def test_an_invented_source_becomes_text_and_is_reported(self):
        c = validate_answer(
            "It is fast [benchmarks.pdf#page=9] and offline [guide#Offline].", self.SOURCES
        )
        assert c.text == "It is fast benchmarks.pdf#page=9 and offline [guide#Offline]."
        assert c.rejected == ["benchmarks.pdf#page=9"]
        assert c.cited == ["guide#Offline"]
        assert not c.clean

    def test_a_dropped_directory_still_resolves(self):
        c = validate_answer("See [q3.pdf#page=2].", self.SOURCES)
        assert c.cited == ["reports/q3.pdf#page=2"]
        assert c.text == "See [reports/q3.pdf#page=2]."

    def test_a_suffix_that_is_not_a_boundary_does_not_resolve(self):
        c = validate_answer("See [3.pdf#page=3].", self.SOURCES)
        assert c.rejected == ["3.pdf#page=3"]

    def test_numbered_footnotes(self):
        c = validate_answer(
            "A [q3.pdf#page=3]. B [guide#Offline]. A again [q3.pdf#page=3].", self.SOURCES
        )
        text, order = c.numbered()
        assert text == "A [1]. B [2]. A again [1]."
        assert order == ["q3.pdf#page=3", "guide#Offline"]

    def test_sources_may_be_strings_dicts_or_results(self):
        class Hit:
            citation = "a.pdf#page=1"

        c = validate_answer(
            "[a.pdf#page=1] [b] [c.md#Top]", [Hit(), "b", {"_vx_citation": "c.md#Top"}]
        )
        assert c.cited == ["a.pdf#page=1", "b", "c.md#Top"]

    def test_the_instruction_names_the_bracket_convention(self):
        assert (
            "square brackets" in CITATION_INSTRUCTION
            and "[report.pdf#page=3]" in CITATION_INSTRUCTION
        )

    def test_cited_is_a_plain_dataclass(self):
        assert Cited(text="t").clean is True


class TestThroughVectrix:
    @pytest.fixture
    def db(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("cited", path=str(tmp_path), mode="dense")
        yield db
        db.close()

    def test_chunks_carry_a_citation_and_results_expose_it(self, db):
        db.add_document(GUIDE, chunk="markdown", metadata={"source": "guide"})
        hits = db.search("does it need the network", limit=3)
        assert hits.top.citation == "guide#Offline"
        assert all("_vx_citation" in h.metadata for h in hits)
        assert hits.citations()[0] == "guide#Offline"
        assert len(hits.citations()) == len(set(hits.citations()))

    def test_a_paged_document_cites_its_page(self, db):
        text = "First page about basalt.\n\nSecond page about sourdough bread and wild yeast."
        doc = LoadedDocument(
            text=text,
            metadata={"source": "/tmp/notes.pdf", "filename": "notes.pdf", "kind": "pdf"},
            pages=[(0, 25), (25, len(text))],
        )
        db.add_document(doc, chunk="sentence", chunk_size=60, overlap=0)
        hit = db.search("sourdough", limit=1).top
        assert hit.citation.startswith("notes.pdf#page=")

    def test_sources_block_and_validation_round_trip(self, db):
        db.add_document(GUIDE, chunk="markdown", metadata={"source": "guide"})
        hits = db.search("network", limit=3)
        block = hits.sources()
        assert block.startswith("[guide#")
        assert "\n\n" in block or len(hits) == 1
        first = block.split("\n\n")[0]
        assert "\n" not in first, "one source is one line"
        assert hits.sources(max_chars=len(first) + 1) == first
        answer = f"Offline is a hard rule [{hits.top.citation}]. Made up [nowhere.pdf#page=1]."
        checked = validate_answer(answer, hits)
        assert checked.cited == [hits.top.citation]
        assert checked.rejected == ["nowhere.pdf#page=1"]

    def test_a_plain_add_falls_back_to_the_id(self, db):
        db.add(["a text with no document behind it"], ids=["loose-1"])
        assert db.search("text", limit=1).top.citation == "loose-1"
