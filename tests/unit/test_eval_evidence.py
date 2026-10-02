"""A golden row's evidence: the exact words that answer, found again, and scored by in chunking and in retrieval.

The page a question names says where its answer is; the quotes the golden
writer checked it against say what the answer is. These hold that the quotes
are kept and read back, checked like every other field, found word for word
the way the writer found them, and that a run asked to score by them wants
the words and not only the page.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vectrixdb._eval_evidence import evidence_found, evidence_span, holds, usable
from vectrixdb.evaluation import Question, check_golden, load_questions, read_golden, retrieval_report, run_chunking_build, save_questions
from vectrixdb.ingest import LoadedDocument

PAGE_ONE = "Emergency fund access. Every fee is waived while cash is needed, for 90 days. The branch hours change in winter."
PAGE_TWO = "Loan payment deferment. A late payment has no fee during deferment, which lasts 1,200 days at most."
TEXT = f"# Fund\n\n{PAGE_ONE}\n\n# Loans\n\n{PAGE_TWO}\n"
DOC = LoadedDocument(text=TEXT, pages=[(0, 1), (TEXT.index("# Loans"), 2)], headings=[(0, "Fund", 1), (TEXT.index("# Loans"), "Loans", 1)])
FEE = Question("Which fee is waived when cash is needed?", reference="Every fee.", expected=["guide.pdf#page=1"], id="q1", evidence=["Every fee is waived while cash is needed"])


def hit(start, end, doc="guide.pdf", text=None, page=None):
    """A search result as a build hands it over: its text, its document and where in it it came from."""
    meta = {"_vx_doc": doc, "_vx_start": start, "_vx_end": end}
    if page is not None:
        meta["page"] = page
    return SimpleNamespace(id=f"{doc}:{start}", text=TEXT[start:end] if text is None else text, metadata=meta)


class TestFindingTheWords:
    def test_word_for_word_means_what_the_writer_meant(self):
        at = evidence_span("every FEE is waived,  while cash\nis needed", TEXT)
        assert at is not None and TEXT[at[0] : at[1]] == "Every fee is waived while cash is needed"
        assert evidence_span("lasts 1200 days at most", TEXT) is not None, "a number without its thousands commas"
        assert evidence_span("Every fee is waived ... for 90 days", TEXT) is not None, "a gap marked with an ellipsis"
        assert evidence_span("Every fee is charged", TEXT) is None

    def test_a_quote_too_short_proves_nothing(self):
        assert not usable("the fee") and usable("no fee during")
        assert evidence_span("the fee", TEXT) is None and not holds(TEXT, "the fee")

    def test_it_is_found_after_where_it_is_looked_for(self):
        again = TEXT + "Every fee is waived while cash is needed."
        assert evidence_span(FEE.evidence[0], again, start=len(TEXT))[0] == len(TEXT)

    def test_a_chunk_holds_a_quote_whole_or_half_of_it_in_a_row(self):
        quote = "A late payment has no fee during deferment, which lasts 1,200 days at most"
        assert holds(PAGE_TWO, quote)
        assert holds("Loan payment deferment. A late payment has no fee during", quote), "the chunk ends inside the quote"
        assert not holds("A late payment has no fee.", quote), "three words of fourteen are not the answer"


class TestTheGoldenRow:
    def test_it_is_read_back_and_written_again(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            json.dumps({"question": "Which fee is waived?", "expected": ["guide.pdf#page=1"], "evidence": ["Every fee is  waived\nwhile cash is needed"]}) + "\n"
            + json.dumps({"question": "How long is deferment?", "expected": ["guide.pdf#page=2"]}) + "\n",
            encoding="utf-8",
        )
        first, second = load_questions(path)
        assert first.evidence == ["Every fee is waived while cash is needed"] and second.evidence == []
        save_questions([first, second], tmp_path / "again.jsonl")
        rows = [json.loads(line) for line in (tmp_path / "again.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["evidence"] == first.evidence and "evidence" not in rows[1], "a row without evidence is written as before"

    def test_it_is_checked_like_every_other_field(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            json.dumps({"question": "One?", "expected": ["a.pdf"], "evidence": ["Every fee is waived"]}) + "\n"
            + json.dumps({"question": "Two?", "expected": ["a.pdf"], "evidence": "Every fee is waived"}) + "\n"
            + json.dumps({"question": "Three?", "expected": ["a.pdf"], "evidence": [3]}) + "\n",
            encoding="utf-8",
        )
        said = [str(p) for p in check_golden(path).errors]
        assert said == [
            'line 2  evidence should be a list, not text, like ["Every fee is waived"]',
            "line 3  evidence should hold quotes as text, not a number",
        ]

    def test_a_quote_too_short_is_a_note_not_a_mistake(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(json.dumps({"question": "One?", "expected": ["a.pdf"], "evidence": ["the fee"]}) + "\n", encoding="utf-8")
        check = check_golden(path)
        assert check.ok and any("fewer than three words, on line 1" in str(p) for p in check.warnings)
        assert read_golden(path).questions[0].evidence == ["the fee"], "kept as written; it is only not used"


class TestChunkingByTheWords:
    DOCS = {"guide.pdf": DOC}

    def test_the_words_handed_over_count_and_the_page_alone_does_not(self):
        start = TEXT.index("Emergency")
        cut = TEXT.index("The branch")
        # Page one handed over, but only the part without the answer in it.
        assert evidence_found(FEE, [(hit(cut, cut + 40), TEXT[cut : cut + 40])], self.DOCS) is False
        assert evidence_found(FEE, [(hit(start, cut), TEXT[start:cut])], self.DOCS) is True

    def test_a_quote_cut_across_two_chunks_is_found_when_both_are_handed(self):
        at = TEXT.index("waived")
        first, second = hit(0, at + 3), hit(at, at + 60)
        assert evidence_found(FEE, [(first, first.text)], self.DOCS) is False
        assert evidence_found(FEE, [(second, second.text), (first, first.text)], self.DOCS) is True, "in any order"

    def test_a_chunk_cut_short_by_the_budget_reaches_only_as_far_as_it_was_handed(self):
        whole = hit(TEXT.index("Emergency"), TEXT.index("The branch"))
        assert evidence_found(FEE, [(whole, whole.text[:30])], self.DOCS) is False

    def test_no_usable_evidence_leaves_it_to_the_pages(self):
        plain = Question("Which fee?", expected=["guide.pdf#page=1"], id="q2")
        short = Question("Which fee?", expected=["guide.pdf#page=1"], id="q3", evidence=["the fee"])
        assert evidence_found(plain, [], self.DOCS) is None and evidence_found(short, [], self.DOCS) is None

    def test_a_build_scores_the_words_where_they_are_quoted(self, tmp_path):
        import numpy as np

        def embed(texts):
            out = np.full((len(texts), 2), 0.1, dtype=np.float32)
            for i, text in enumerate(texts):
                out[i, 0] += text.lower().count("fee")
                out[i, 1] += text.lower().count("loan")
            return out

        wrong_words = Question("Which fee is waived?", expected=["guide.pdf#page=1"], id="q4", evidence=["The branch hours change in winter"])
        build = {"key": "markdown-600-h", "technique": "markdown", "chunk": "markdown", "size": 600, "overlap": 120, "headings": True, "parent_size": None}
        result = run_chunking_build({"guide.pdf": DOC}, [FEE, wrong_words], build, budget=90, open_with={"embed_fn": embed, "dimension": 2}, workdir=tmp_path)
        assert result["by_evidence"] == 2
        assert result["outcomes"]["q1"][0] is True, "the words that answer were in the ninety characters"
        assert result["outcomes"]["q4"][0] is False, "page one came back, but not these words"


class _Index:
    """A collection that answers every question with the same two chunks of page one: the page first, then the words."""

    def __init__(self):
        cut = TEXT.index("The branch")
        self.hits = [hit(cut, len(TEXT.split("# Loans")[0]), page=1), hit(TEXT.index("Emergency"), cut, page=1)]

    def search(self, text, limit=10, **how):
        return self.hits[:limit]


class TestRetrievalByTheWords:
    def test_by_evidence_wants_the_chunk_that_holds_the_words(self):
        by_page = retrieval_report(_Index(), [FEE], k=(1, 2), by="doc")
        by_words = retrieval_report(_Index(), [FEE], k=(1, 2), by="evidence")
        assert by_page["recall"]["@1"] == 1.0, "any chunk of page one is right by its page"
        assert by_words["recall"] == {"@1": 0.0, "@2": 1.0} and by_words["mrr"] == 0.5 and by_words["by"] == "evidence"

    def test_a_question_without_evidence_is_counted_by_its_page(self):
        plain = Question("Which fee?", expected=["guide.pdf#page=1"], id="q2")
        assert retrieval_report(_Index(), [plain], k=(1,), by="evidence")["recall"]["@1"] == 1.0

    def test_a_setup_is_ranked_by_the_words_too(self):
        from vectrixdb.evaluation import run_setup

        assert run_setup(_Index(), {"key": "x", "search": {}}, [FEE], by="evidence")["ranks"] == [2]
        assert run_setup(_Index(), {"key": "x", "search": {}}, [FEE], by="doc")["ranks"] == [1]

    def test_evaluate_refuses_a_way_it_does_not_know(self):
        from vectrixdb.evaluation import evaluate

        with pytest.raises(ValueError, match="'doc', 'chunk' or 'evidence'"):
            evaluate(_Index(), [FEE], by="page")
