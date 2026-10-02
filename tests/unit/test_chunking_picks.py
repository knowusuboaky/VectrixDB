"""A pick, as the ways of cutting and of searching are named: a build key and a pinned way of searching.

What is held to. A build key names a build whether or not a run tried it,
and anything else is refused with what a key is. A build becomes the options
add_document and rechunk take, late said either way, and a model-built cut
is refused without its model. A question is handed what a run hands it: the
limit and the parents are the build's, the way of searching the caller's or
the run's own, best first until the budget. A run's pick is its best build,
ranked as the report ranks them, and
the build in use stays when the questions only one of the two got right
split the way luck could. A pinned way of searching reads the same in every
collection, and keyword search takes no vectors.
"""

from __future__ import annotations

import pytest

from vectrixdb.evaluation import TECHNIQUES, chunking_build, chunking_choice, chunking_options, chunking_plan, handed_over, search_of


def build(key, outcomes, chunks=100):
    """One build as a run makes it: its key, how many it was asked, and each question's outcome, [found, answered right]."""
    one = chunking_build(key)
    return {
        **one,
        "questions": len(outcomes),
        "chunks": chunks,
        "found": sum(1 for o in outcomes.values() if o[0]),
        "answered": sum(1 for o in outcomes.values() if o[1]),
        "outcomes": outcomes,
        "error": None,
    }


def right(first, last):
    """Questions q<first> to q<last - 1> answered right, every other of 100 missed."""
    return {f"q{i}": [first <= i < last, first <= i < last] for i in range(100)}


class TestABuildKey:
    def test_names_the_technique_the_size_and_what_goes_in_front(self):
        assert chunking_build("markdown-1000-h") == {
            "key": "markdown-1000-h", "technique": "markdown", "chunk": "markdown", "size": 1000, "overlap": 200,
            "headings": True, "adds": "headings", "parent_size": None,
        }
        assert chunking_build("parent-500-n")["parent_size"] == 2000
        assert chunking_build("recursive-2000-c")["headings"] is True, "a note goes in front of the headings, as the run builds it"
        assert chunking_build("fixed-500-l")["adds"] == "late"

    def test_a_size_no_run_tries_is_a_build_too(self):
        assert chunking_build("sentence-1500-n")["overlap"] == 300

    def test_every_build_a_plan_makes_reads_back_the_same(self):
        for one in chunking_plan(list(TECHNIQUES), context=True, late=True):
            assert chunking_build(one["key"]) == one

    @pytest.mark.parametrize("wrong", ["markdown", "markdown-1000", "markdown-big-h", "markdown-1000-x", "chapters-1000-h", "", "markdown-1000-h-extra"])
    def test_anything_else_is_refused_with_what_a_key_is(self, wrong):
        with pytest.raises(ValueError, match="markdown-1000-h"):
            chunking_build(wrong)

    def test_too_small_to_cut(self):
        with pytest.raises(ValueError, match="too small"):
            chunking_build("fixed-20-n")


class TestItsOptions:
    def test_what_add_document_is_given(self):
        assert chunking_options("markdown-1000-h") == {"chunk": "markdown", "chunk_size": 1000, "overlap": 200, "embed_heading": True, "parent_size": None, "late": False}
        assert chunking_options(chunking_build("parent-250-n")) == {"chunk": "markdown", "chunk_size": 250, "overlap": 50, "embed_heading": False, "parent_size": 2000, "late": False}
        assert chunking_options("semantic-500-l")["late"] is True

    def test_a_cut_that_is_not_late_turns_late_off(self, tmp_path):
        # rechunk() keeps what it is not given, so a build must say late either way.
        import numpy as np

        from vectrixdb import Vectrix
        from vectrixdb.documents import DocumentStore, LocalFiles

        def embed(texts):
            out = np.ones((len(texts), 8), dtype=np.float32)
            return out / np.linalg.norm(out, axis=1, keepdims=True)

        db = Vectrix("docs", path=str(tmp_path / "db"), embed_fn=embed, dimension=8, keep_source=DocumentStore(LocalFiles(tmp_path / "md")), markdown_first=True)
        try:
            db.add_document("# Terms\n\nPayment is due within thirty days of the invoice.\n", doc_id="msa.md", index=False)
            late = {"chunk": "markdown", "chunk_size": 500, "overlap": 100, "parent_size": None, "embed_heading": False, "late": True}
            db.documents.put("msa.md", db.documents.get("msa.md"), chunking=late)
            db.rechunk("msa.md", **chunking_options("markdown-500-n"))
            assert "late" not in db.documents.entry("msa.md")["chunking"]
        finally:
            db.close()

    def test_a_model_built_cut_needs_its_model(self):
        cutter, noter = object(), object()
        with pytest.raises(ValueError, match="cut_with"):
            chunking_options("llm-1000-h")
        assert chunking_options("llm-1000-h", cut_with=cutter)["cut_with"] is cutter
        with pytest.raises(ValueError, match="context_with"):
            chunking_options("markdown-1000-c")
        assert chunking_options("markdown-1000-c", context_with=noter)["context_with"] is noter


class Hit:
    def __init__(self, id, text):
        self.id, self.text, self.metadata = id, text, {}


class Searched:
    """A collection that says what it was asked, and gives back the hits it was made with."""

    def __init__(self, hits):
        self.hits, self.asked = hits, []

    def search(self, query, **kwargs):
        self.asked.append((query, kwargs))
        return list(self.hits)


class TestWhatAQuestionIsHanded:
    def test_as_a_run_asks_hybrid_with_no_reranker_enough_to_fill_the_budget(self):
        db = Searched([Hit("a", "x" * 100)])
        handed_over(db, "net income", "markdown-1000-h")
        assert db.asked == [("net income", {"limit": 12, "parents": False, "mode": "hybrid", "rerank": False})], "6,000 characters at 500 a result"

    def test_a_parent_child_build_hands_over_sections(self):
        db = Searched([Hit("a", "x")])
        handed_over(db, "q", "parent-250-n")
        assert db.asked[0][1]["parents"] is True

    def test_a_way_a_retrieval_run_picked_is_searched_as_it_is(self):
        db = Searched([Hit("a", "x")])
        handed_over(db, "q", "markdown-1000-h", search={"mode": "dense", "vectors": "azure", "limit": 3, "parents": True})
        assert db.asked[0][1] == {"limit": 12, "parents": False, "mode": "dense", "vectors": "azure"}, "the build decides the limit and the parents"

    def test_best_first_until_the_budget_the_last_cut_to_fit_and_each_once(self):
        db = Searched([Hit("a", "a" * 400), Hit("a", "a" * 400), Hit("b", "b" * 400), Hit("c", "c" * 400)])
        handed = handed_over(db, "q", "markdown-500-n", budget=1000)
        assert [(hit.id, len(text)) for hit, text in handed] == [("a", 400), ("b", 400), ("c", 200)]


class TestTheRunsPick:
    def test_the_best_when_nothing_was_in_use(self):
        choice = chunking_choice([build("markdown-1000-h", right(0, 60)), build("fixed-500-n", right(0, 40))])
        assert (choice["pick"], choice["switch"]) == ("markdown-1000-h", True)

    def test_the_one_in_use_stays_while_it_is_the_best(self):
        choice = chunking_choice([build("markdown-1000-h", right(0, 60)), build("fixed-500-n", right(0, 40))], "markdown-1000-h")
        assert (choice["pick"], choice["switch"]) == ("markdown-1000-h", False)

    def test_a_win_luck_could_make_does_not_switch(self):
        # Two questions only the new best got right, and none the other way: a coin could do that.
        choice = chunking_choice([build("recursive-500-n", right(0, 52)), build("markdown-1000-h", right(0, 50))], "markdown-1000-h")
        assert (choice["pick"], choice["switch"]) == ("markdown-1000-h", False)
        assert "luck could do" in choice["why"]

    def test_a_clear_win_switches_and_says_by_how_much(self):
        choice = chunking_choice([build("recursive-500-n", right(0, 80)), build("markdown-1000-h", right(0, 50))], "markdown-1000-h")
        assert (choice["pick"], choice["switch"]) == ("recursive-500-n", True)
        assert "30 right" in choice["why"] and "more than luck" in choice["why"]

    def test_ranked_as_the_report_ranks_them_fewer_chunks_first_on_a_tie(self):
        choice = chunking_choice([build("markdown-1000-h", right(0, 60), chunks=300), build("markdown-2000-h", right(0, 60), chunks=150)])
        assert choice["pick"] == "markdown-2000-h"

    def test_the_one_in_use_not_built_is_replaced(self):
        choice = chunking_choice([build("fixed-500-n", right(0, 40))], "sentence-1500-n")
        assert (choice["pick"], choice["switch"]) == ("fixed-500-n", True) and "not among" in choice["why"]

    def test_nothing_ran_so_nothing_moves(self):
        failed = {**build("fixed-500-n", right(0, 40)), "error": "it broke"}
        assert chunking_choice([failed], "markdown-1000-h") == {"pick": "markdown-1000-h", "switch": False, "why": "no build ran, so nothing was compared"}

    def test_found_decides_when_no_model_answered(self):
        found_only = [
            {**build("recursive-500-n", right(0, 80)), "answered": None, "outcomes": {q: [o[0], None] for q, o in right(0, 80).items()}},
            {**build("markdown-1000-h", right(0, 50)), "answered": None, "outcomes": {q: [o[0], None] for q, o in right(0, 50).items()}},
        ]
        assert chunking_choice(found_only, "markdown-1000-h")["switch"] is True


class TestAPinnedWayOfSearching:
    def test_the_method_and_its_vectors(self):
        assert search_of("hybrid_semantic.both") == {"mode": "hybrid", "rerank": "semantic", "vectors": "both"}
        assert search_of("dense.azure") == {"mode": "dense", "vectors": "azure"}
        assert search_of("hybrid") == {"mode": "hybrid", "rerank": False}, "no vectors named, the collection's own"
        assert search_of("keyword_semantic") == {"mode": "sparse", "rerank": "semantic"}

    def test_keyword_search_takes_no_vectors(self):
        with pytest.raises(ValueError, match="no vectors"):
            search_of("keyword.both")

    @pytest.mark.parametrize("wrong", ["", "hybridish", "auto", "financial.hybrid.both"])
    def test_a_method_it_does_not_know_says_which_it_knows(self, wrong):
        with pytest.raises(ValueError, match="The methods are"):
            search_of(wrong)
