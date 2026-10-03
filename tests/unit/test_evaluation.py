"""Questions with known answers, read from this library's JSONL or from a
Bedrock evaluation dataset, scored for retrieval and, with a judge you bring,
for the answers.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from vectrixdb.evaluation import (
    ANSWER_METRICS,
    Question,
    answer_report,
    load_bedrock_jsonl,
    load_questions,
    retrieval_report,
    save_questions,
    suggest_expected,
)

BEDROCK = [
    {
        "conversationTurns": [
            {
                "prompt": {
                    "content": [
                        {
                            "text": "A customer lost their home in a wildfire and has no cash. What can the bank offer?"
                        }
                    ]
                },
                "referenceResponses": [
                    {"content": [{"text": "Emergency fund access and fee relief."}]}
                ],
            }
        ]
    },
    {
        "conversationTurns": [
            {
                "prompt": {
                    "content": [
                        {
                            "text": "A customer cannot make their loan payment this month. What options exist?"
                        }
                    ]
                },
                "referenceResponses": [
                    {"content": [{"text": "Payment deferment with no late fees."}]},
                    {"content": [{"text": "Deferred amounts go to the end of the term."}]},
                ],
            }
        ]
    },
]

DOCS = {
    "emergency-fund": "Emergency fund access and fee relief: expedited access to funds and waived fees for displaced customers.",
    "deferment": "Payment deferment: customers in disaster areas can defer loan payments with no late fees.",
    "divestment": "Early divestment guidance for customers who need to sell investments before maturity.",
}
TOPIC = {"emergency-fund": [1, 0, 0], "deferment": [0, 1, 0], "divestment": [0, 0, 1]}
WORDS = {
    "cash": 0,
    "fund": 0,
    "emergency": 0,
    "fee": 0,
    "payment": 1,
    "deferment": 1,
    "loan": 1,
    "late": 1,
    "divestment": 2,
    "investments": 2,
}


def embed(texts):
    out = np.zeros((len(texts), 3), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().replace(".", " ").replace(",", " ").replace(":", " ").split():
            if word in WORDS:
                out[i, WORDS[word]] += 1.0
        if not out[i].any():
            out[i] = [0.1, 0.1, 0.1]
    return out


@pytest.fixture
def db(tmp_path):
    from vectrixdb import Vectrix

    handle = Vectrix("policies", path=str(tmp_path), embed_fn=embed, dimension=3)
    for doc_id, text in DOCS.items():
        handle.add_document(text, doc_id=doc_id)
    yield handle
    handle.close()


@pytest.fixture
def bedrock_file(tmp_path):
    path = tmp_path / "eval.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in BEDROCK) + "\n\n", encoding="utf-8")
    return path


class TestLoading:
    def test_a_bedrock_dataset(self, bedrock_file):
        questions = load_bedrock_jsonl(bedrock_file)
        assert [q.id for q in questions] == ["q1.1", "q2.1"]
        assert (
            questions[0].text.startswith("A customer lost their home")
            and questions[0].expected == []
        )
        assert (
            questions[1].reference
            == "Payment deferment with no late fees. Deferred amounts go to the end of the term."
        )
        assert [q.id for q in load_questions(bedrock_file)] == ["q1.1", "q2.1"], (
            "recognised without being told"
        )

    def test_our_own_format_round_trips(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        save_questions([Question("What is deferred?", "Loan payments.", ["deferment"], "g1")], path)
        (back,) = load_questions(path)
        assert back == Question("What is deferred?", "Loan payments.", ["deferment"], "g1")

    def test_a_bad_line_says_which(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"conversationTurns": []}\n{not json\n', encoding="utf-8")
        with pytest.raises(ValueError, match="line 2: not JSON"):
            load_bedrock_jsonl(path)


class TestRetrieval:
    def test_labels_are_suggested_from_the_reference_answer_and_never_applied(
        self, db, bedrock_file
    ):
        questions = load_bedrock_jsonl(bedrock_file)
        suggested = suggest_expected(db, questions)
        assert suggested == {"q1.1": ["emergency-fund"], "q2.1": ["deferment"]}
        assert all(q.expected == [] for q in questions)

    def test_recall_mrr_and_ndcg(self, db, bedrock_file):
        questions = load_bedrock_jsonl(bedrock_file)
        for q, label in zip(questions, (["emergency-fund"], ["deferment"])):
            q.expected = label
        questions.append(Question("Where is the branch?", "", [], "q3"))
        report = retrieval_report(db, questions, k=(1, 3), mode="dense")
        assert report["labelled"] == 2 and report["unlabelled"] == 1
        assert (
            report["recall"] == {"@1": 1.0, "@3": 1.0}
            and report["mrr"] == 1.0
            and report["ndcg@3"] == 1.0
        )
        assert report["misses"] == [] and report["search"] == {"mode": "dense"}

    def test_ndcg_never_passes_one_when_two_chunks_answer_one_entry(self):
        """Two chunks quoting the evidence both count as right; the ideal
        ranking has to hold two right answers too, or nDCG comes out above 1."""
        from types import SimpleNamespace

        class Hits:
            def search(self, q, limit=10, **kw):
                return [
                    SimpleNamespace(
                        id="r:0",
                        metadata={"_vx_doc": "r"},
                        text="the fee is two percent",
                        score=1.0,
                    ),
                    SimpleNamespace(
                        id="r:1",
                        metadata={"_vx_doc": "r"},
                        text="again: the fee is two percent",
                        score=0.9,
                    ),
                ][:limit]

        q = Question(text="q", expected=["r"], evidence=["the fee is two percent"])
        report = retrieval_report(Hits(), [q], k=(1, 3), by="evidence")
        assert report["ndcg@3"] == 1.0

    def test_a_miss_is_listed_with_what_came_back(self, db):
        report = retrieval_report(
            db,
            [Question("How do I defer a loan payment?", "", ["divestment"], "wrong-label")],
            k=(1,),
        )
        assert report["recall"] == {"@1": 0.0} and report["mrr"] == 0.0
        assert (
            report["misses"][0]["id"] == "wrong-label"
            and report["misses"][0]["got"][0] == "deferment"
        )

    def test_no_labels_is_not_a_perfect_score(self, db):
        report = retrieval_report(db, [Question("anything", "", [], "q")])
        assert report["labelled"] == 0 and report["mrr"] is None and report["recall"]["@1"] is None

    def test_by_chunk_wants_chunk_ids(self, db):
        report = retrieval_report(
            db, [Question("loan payment deferment", "", ["deferment:0"], "q")], k=(1,), by="chunk"
        )
        assert report["recall"] == {"@1": 1.0}
        with pytest.raises(ValueError, match="'doc', 'chunk' or 'evidence'"):
            retrieval_report(db, [], by="page")


class TestAnswers:
    def test_a_judge_scores_the_four_bedrock_metrics(self):
        questions = [
            Question("What is deferred?", "Loan payments.", id="g1"),
            Question("Unanswered", "", id="g2"),
        ]
        seen = []

        def judge(metric, question, reference, answer, sources):
            seen.append((metric, sources))
            return 1.0 if metric == "correctness" else 0.5

        report = answer_report(
            questions,
            {"g1": {"answer": "Loan payments are deferred.", "sources": ["deferment"]}},
            judge,
        )
        assert report["scored"] == 1 and report["unanswered"] == 1
        assert report["metrics"] == {
            "correctness": 1.0,
            "completeness": 0.5,
            "faithfulness": 0.5,
            "helpfulness": 0.5,
        }
        assert [m for m, _ in seen] == list(ANSWER_METRICS) and seen[0][1] == ["deferment"]

    def test_a_judge_that_has_not_understood_is_an_error(self):
        with pytest.raises(ValueError, match="a score is between 0 and 1"):
            answer_report([Question("q", id="g1")], {"g1": "a"}, lambda **k: 7)
        with pytest.raises(ValueError, match="metrics are correctness"):
            answer_report([], {}, lambda **k: 1.0, metrics=["vibes"])
