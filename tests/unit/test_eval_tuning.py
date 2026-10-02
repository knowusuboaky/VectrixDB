"""Where to stop answering, and how to cut documents: the two measurements a golden file settles."""

from __future__ import annotations

import numpy as np
import pytest

from vectrixdb.evaluation import Question, answer_cutoff, sweep, sweep_markdown


class Hit:
    def __init__(self, doc, similarity, relevance=None):
        self.id, self.metadata = f"{doc}:0", {"_vx_doc": doc}
        self.similarity, self.relevance = (
            similarity,
            relevance if relevance is not None else similarity,
        )


class Canned:
    """A collection that answers each question with the hit it was given."""

    def __init__(self, answers):
        self.answers, self.asked, self.model_name = answers, [], "a-model"

    def search(self, text, limit=1, **how):
        self.asked.append((text, how))
        hit = self.answers.get(text)
        return [hit] if hit is not None else []


def golden(n_right, n_wrong, right=0.8, wrong=0.6):
    questions, answers = [], {}
    for i in range(n_right):
        questions.append(Question(f"right {i}", expected=["a"], id=f"r{i}"))
        answers[f"right {i}"] = Hit("a", right + i * 0.01)
    for i in range(n_wrong):
        questions.append(Question(f"wrong {i}", expected=["a"], id=f"w{i}"))
        answers[f"wrong {i}"] = Hit("b", wrong - i * 0.01)
    return questions, answers


class TestAnswerCutoff:
    def test_the_cutoff_sits_between_the_answers_and_the_near_misses(self):
        questions, answers = golden(6, 6)
        found = answer_cutoff(Canned(answers), questions)
        assert 0.6 < found["cutoff"] < 0.8
        assert found["at_cutoff"] == {
            "cutoff": found["cutoff"],
            "precision": 1.0,
            "answered": 1.0,
            "declined": 1.0,
        }
        assert found["separation"] == 1.0 and found["answers"] == 6 and found["near_misses"] == 6
        assert found["model"] == "a-model" and found["measure"] == "similarity"

    def test_when_they_overlap_it_says_how_well_they_separate_and_what_each_choice_costs(self):
        questions, answers = golden(8, 8, right=0.70, wrong=0.74)
        found = answer_cutoff(Canned(answers), questions)
        assert 0.5 < found["separation"] < 1.0
        at = found["at_cutoff"]
        assert at["answered"] < 1.0 or at["declined"] < 1.0, (
            "no cut-off keeps every answer and declines every miss"
        )
        cuts = [row["cutoff"] for row in found["table"]]
        assert cuts == sorted(cuts) and found["cutoff"] in cuts and len(cuts) <= 15
        strict = found["table"][-1]
        assert strict["declined"] == 1.0, "the last row is the strictest there is"

    def test_a_question_the_documents_do_not_answer_is_a_near_miss_whatever_came_top(self):
        questions, answers = golden(6, 0)
        outside = [f"zebras {i}" for i in range(5)]
        answers.update({text: Hit("a", 0.55) for text in outside})
        found = answer_cutoff(Canned(answers), questions, unanswerable=outside)
        assert (
            found["near_misses"] == 5
            and found["unanswerable"] == 5
            and 0.55 < found["cutoff"] <= 0.8
        )

    def test_too_few_of_either_is_no_cutoff_and_says_which(self):
        questions, answers = golden(6, 2)
        found = answer_cutoff(Canned(answers), questions)
        assert (
            found["cutoff"] is None
            and "too few near misses" in found["reason"]
            and "unanswerable=" in found["reason"]
        )
        questions, answers = golden(2, 6)
        found = answer_cutoff(Canned(answers), questions)
        assert found["cutoff"] is None and "too few answers" in found["reason"]

    def test_results_with_nothing_to_measure_say_so(self):
        questions, answers = golden(6, 6)
        for hit in answers.values():
            hit.similarity = None
        found = answer_cutoff(Canned(answers), questions)
        assert (
            found["cutoff"] is None
            and found["unmeasured"] == 12
            and "keyword search has none" in found["reason"]
        )

    def test_the_search_is_the_callers_and_the_measure_can_be_the_rerankers(self):
        questions, answers = golden(6, 6)
        for text, hit in answers.items():
            hit.relevance = 0.9 if text.startswith("right") else 0.1
        db = Canned(answers)
        found = answer_cutoff(db, questions, measure="relevance", mode="hybrid", rerank=True)
        assert 0.1 < found["cutoff"] <= 0.9 and found["search"] == {
            "mode": "hybrid",
            "rerank": True,
        }
        assert all(how == {"mode": "hybrid", "rerank": True} for _, how in db.asked)

    def test_unlabelled_questions_are_left_out_and_wrong_arguments_refused(self):
        questions, answers = golden(6, 6)
        found = answer_cutoff(Canned(answers), [*questions, Question("no label")])
        assert found["questions"] == 12
        with pytest.raises(ValueError, match="measure is one of"):
            answer_cutoff(Canned({}), [], measure="score")
        with pytest.raises(ValueError, match="by is"):
            answer_cutoff(Canned({}), [], by="page")


DOCS = {
    "emergency-fund": "# Emergency fund\n\nEmergency fund access and fee relief: expedited access to funds and waived fees for displaced customers.\n\n## Fees\n\nEvery fee is waived while the cash is needed.",
    "deferment": "# Deferment\n\nPayment deferment: customers in disaster areas can defer loan payments with no late fees.\n\n## Term\n\nThe deferred payment goes to the end of the loan.",
    "divestment": "# Divestment\n\nEarly divestment guidance for customers who need to sell investments before maturity.",
}
WORDS = {
    "cash": 0,
    "fund": 0,
    "emergency": 0,
    "fee": 0,
    "fees": 0,
    "payment": 1,
    "deferment": 1,
    "loan": 1,
    "late": 1,
    "divestment": 2,
    "investments": 2,
}
QUESTIONS = [
    Question("Which fee is waived when cash is needed?", expected=["emergency-fund"], id="g1"),
    Question("Can a loan payment be late under deferment?", expected=["deferment"], id="g2"),
    Question("What about selling investments, divestment?", expected=["divestment"], id="g3"),
    Question("Nobody labelled this one"),
]


def embed(texts):
    out = np.zeros((len(texts), 3), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in "".join(c if c.isalpha() else " " for c in text.lower()).split():
            if word in WORDS:
                out[i, WORDS[word]] += 1.0
        if not out[i].any():
            out[i] = [0.1, 0.1, 0.1]
    return out


OURS = {"embed_fn": embed, "dimension": 3}


class TestSweep:
    def test_every_combination_is_built_asked_and_ranked(self, tmp_path):
        seen = []
        report = sweep(
            DOCS,
            QUESTIONS,
            chunk=["recursive", "markdown"],
            chunk_size=[80, 400],
            overlap=[20],
            embed_heading=[False, True],
            k=(1, 3),
            open_with=OURS,
            workdir=tmp_path,
            progress=lambda row, n, total: seen.append((n, total)),
        )
        assert report["builds"] == 8 and len(report["rows"]) == 8 and seen[-1] == (8, 8)
        assert report["questions"] == 3 and report["documents"] == 3 and report["k"] == [1, 3]
        assert [row["rank"] for row in report["rows"]] == list(range(1, 9)) and report[
            "best"
        ] is report["rows"][0]
        scores = [row["ndcg"] for row in report["rows"]]
        assert scores == sorted(scores, reverse=True)
        combos = {(r["chunk"], r["chunk_size"], r["embed_heading"]) for r in report["rows"]}
        assert len(combos) == 8
        for row in report["rows"]:
            assert (
                row["recall"]["@3"] == 1.0
                and row["chunks"] >= 3
                and row["median_ms"] is not None
                and row["build_s"] >= 0
            )
        small = next(
            r for r in report["rows"] if r["chunk_size"] == 80 and r["chunk"] == "recursive"
        )
        large = next(
            r for r in report["rows"] if r["chunk_size"] == 400 and r["chunk"] == "recursive"
        )
        assert small["chunks"] > large["chunks"], "a smaller size really cut the documents smaller"

    def test_the_scratch_builds_are_removed_and_the_originals_untouched(self, tmp_path):
        sweep(DOCS, QUESTIONS, open_with=OURS, workdir=tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_each_way_of_searching_is_a_row_of_the_same_build(self, tmp_path):
        report = sweep(
            DOCS,
            QUESTIONS,
            search=[{"mode": "dense"}, {"mode": "hybrid", "rerank": False}],
            open_with=OURS,
            workdir=tmp_path,
        )
        assert report["builds"] == 1 and [
            r["search"] for r in sorted(report["rows"], key=lambda r: r["search"]["mode"])
        ] == [{"mode": "dense"}, {"mode": "hybrid", "rerank": False}]
        assert len({r["chunks"] for r in report["rows"]}) == 1

    def test_files_are_read_from_where_they_are_under_the_ids_given(self, tmp_path):
        folder = tmp_path / "originals"
        folder.mkdir()
        paths = {}
        for doc_id, text in DOCS.items():
            (folder / f"{doc_id}-v2.md").write_text(text, encoding="utf-8")
            paths[doc_id] = str(folder / f"{doc_id}-v2.md")
        report = sweep(paths, QUESTIONS, open_with=OURS, workdir=tmp_path / "scratch")
        assert report["best"]["recall"]["@10"] == 1.0 and report["best"]["misses"] == []

    def test_a_miss_is_named(self, tmp_path):
        questions = [
            *QUESTIONS,
            Question("cash emergency fund fee", expected=["a-document-that-is-not-there"], id="g9"),
        ]
        report = sweep(DOCS, questions, open_with=OURS, workdir=tmp_path)
        assert report["best"]["misses"] == ["g9"] and report["best"]["recall"]["@10"] == 0.75

    def test_an_overlap_as_long_as_the_chunk_is_left_out(self, tmp_path):
        report = sweep(
            DOCS, QUESTIONS, chunk_size=[100, 400], overlap=[100], open_with=OURS, workdir=tmp_path
        )
        assert [r["chunk_size"] for r in report["rows"]] == [400]
        with pytest.raises(ValueError, match="at least as long"):
            sweep(
                DOCS, QUESTIONS, chunk_size=[100], overlap=[100], open_with=OURS, workdir=tmp_path
            )

    def test_what_cannot_be_swept_is_refused_before_anything_is_built(self, tmp_path):
        with pytest.raises(ValueError, match="nothing to score against"):
            sweep(DOCS, [Question("no label")], open_with=OURS, workdir=tmp_path)
        with pytest.raises(ValueError, match="chunk is a list"):
            sweep(DOCS, QUESTIONS, chunk="markdown", open_with=OURS, workdir=tmp_path)
        with pytest.raises(ValueError, match="documents is empty"):
            sweep({}, QUESTIONS, open_with=OURS, workdir=tmp_path)
        with pytest.raises(ValueError, match="dense, sparse, hybrid or ultimate"):
            sweep(DOCS, QUESTIONS, search=[{"mode": "graph"}], open_with=OURS, workdir=tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_the_table_is_best_first_and_says_how_each_was_searched(self, tmp_path):
        report = sweep(
            DOCS,
            QUESTIONS,
            chunk=["recursive", "markdown"],
            search=[{"mode": "hybrid", "rerank": False}],
            k=(1, 3),
            open_with=OURS,
            workdir=tmp_path,
        )
        lines = sweep_markdown(report).splitlines()
        assert lines[0].startswith(
            "| # | chunker | size | overlap | headings | search | recall @1 | recall @3 | MRR | nDCG |"
        )
        assert (
            len(lines) == 4
            and lines[2].startswith("| 1 | ")
            and "hybrid (rerank=False)" in lines[2]
        )
