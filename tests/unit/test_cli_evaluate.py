"""vectrixdb golden template, golden write and vectrixdb evaluate, from a terminal.

The template writes rows to fill in from the collection's own documents, and
golden write drafts the questions with the chat model the settings name; the
evaluation searches, ranks, names three picks, and saves the run where the
server's Evaluate pages read it, the data path's evaluations folder.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from vectrixdb.cli import app

TEXTS = {
    "deferment": "Payment deferment: customers in disaster areas can defer loan payments with no late fees.",
    "emergency-fund": "Emergency fund access and fee relief: expedited access to funds for displaced customers.",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Offline, and no setting left behind: an env file writes straight into the environment."""
    import os

    before = {n for n in os.environ if n.startswith("VECTRIXDB_")}
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
    yield
    for name in [n for n in os.environ if n.startswith("VECTRIXDB_") and n not in before]:
        del os.environ[name]


@pytest.fixture
def db_path(tmp_path):
    from vectrixdb import Vectrix

    path = tmp_path / "db"
    db = Vectrix("docs", path=str(path))
    for doc_id, text in TEXTS.items():
        db.add_document(text, doc_id=doc_id)
    db.close()
    return path


def test_the_template_names_the_documents_and_leaves_the_questions(tmp_path, db_path):
    out = tmp_path / "golden.jsonl"
    result = CliRunner().invoke(app, ["golden", "template", "docs", "--path", str(db_path), "--out", str(out), "-n", "5"])
    assert result.exit_code == 0, result.output
    assert "Wrote 2 rows" in result.output
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["expected"][0] for r in rows) == sorted(TEXTS) and all(r["question"] == "" and r["hint"] for r in rows)


def test_evaluate_ranks_picks_and_saves_where_the_server_reads(tmp_path, db_path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps({"id": "g1", "question": "Can I postpone a loan payment?", "expected": ["deferment"]}) + "\n"
        + json.dumps({"id": "g2", "question": "", "expected": ["emergency-fund"], "hint": "Emergency fund access"}) + "\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["evaluate", "docs", "--golden", str(golden), "--path", str(db_path), "--mode", "dense"])
    assert result.exit_code == 0, result.output
    assert "1 questions from" in result.output and "1 template rows still empty" in result.output
    for pick in ("finds_the_most:", "best_for_balance:", "best_for_time:"):
        assert pick in result.output
    assert "Nothing was switched" in result.output
    runs = list((db_path / "evaluations" / "retrieval" / "runs").iterdir())
    assert len(runs) == 1 and [p.name for p in runs[0].iterdir()] == ["report.json"]


def test_a_run_goes_where_the_server_with_the_same_settings_reads_runs(tmp_path, db_path, monkeypatch):
    """The server reads VECTRIXDB_EVALUATIONS when it is set, so a run saved anywhere else is never on its pages."""
    monkeypatch.delenv("VECTRIXDB_PATH", raising=False)
    monkeypatch.delenv("VECTRIXDB_EVALUATIONS", raising=False)
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"id": "g1", "question": "Can I postpone a loan payment?", "expected": ["deferment"]}) + "\n", encoding="utf-8")
    runs = tmp_path / "shared-runs"
    env_file = tmp_path / "vectrixdb.env"
    env_file.write_text(f"VECTRIXDB_PATH={db_path}\nVECTRIXDB_EVALUATIONS={runs}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["evaluate", "docs", "--golden", str(golden), "--env-file", str(env_file), "--mode", "dense"])
    assert result.exit_code == 0, result.output
    assert len(list((runs / "retrieval" / "runs").iterdir())) == 1 and not (db_path / "evaluations").exists()
    assert f"to{runs}".replace(" ", "") in "".join(result.output.split()), "the terminal wraps a long path"


def test_a_missing_document_is_said_before_the_searching_starts(tmp_path, db_path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps({"id": "g1", "question": "Can I postpone a loan payment?", "expected": ["deferment"]}) + "\n"
        + json.dumps({"id": "g2", "question": "Where is the old travel policy?", "expected": ["retired-policy"]}) + "\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["evaluate", "docs", "--golden", str(golden), "--path", str(db_path), "--mode", "dense"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    warning = said.index("Warning: VectrixDB has no document called retired-policy, so 1 question (g2) cannot be found by any setup")
    assert warning < said.index("1/2 "), "in time to stop the run and fix the golden file"
    (run,) = (db_path / "evaluations" / "retrieval" / "runs").iterdir()
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    assert report["missing"] == {"VectrixDB": {"documents": ["retired-policy"], "questions": ["g2"]}}


def test_a_golden_file_that_is_not_there_says_so(tmp_path, db_path):
    result = CliRunner().invoke(app, ["evaluate", "docs", "--golden", str(tmp_path / "nope.jsonl"), "--path", str(db_path)])
    assert result.exit_code == 1 and "Error" in result.output


def test_a_collection_that_is_not_there_says_so(tmp_path, db_path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"question": "Where?", "expected": ["deferment"]}) + "\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["evaluate", "missing", "--golden", str(golden), "--path", str(db_path)])
    assert result.exit_code == 1 and "does not exist" in result.output



def flat(result):
    """The output on one line: the console wraps at the terminal's width, wherever that falls."""
    return " ".join(result.output.split())


BEDROCK = {"conversationTurns": [{
    "prompt": {"content": [{"text": "A customer cannot make their loan payment this month. What options exist?"}]},
    "referenceResponses": [{"content": [{"text": "Payment deferment with no late fees for customers in disaster areas."}]}],
}]}


def test_label_proposes_documents_marks_them_draft_and_never_writes_over_a_file(tmp_path, db_path):
    asked = tmp_path / "bedrock.jsonl"
    asked.write_text(json.dumps(BEDROCK) + "\n", encoding="utf-8")
    out = tmp_path / "golden.jsonl"
    result = CliRunner().invoke(app, ["golden", "label", "docs", "--questions", str(asked), "--path", str(db_path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "1 with a proposed label marked draft" in flat(result)
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["expected"] == ["deferment"] and row["draft"] is True and row["reference"].startswith("Payment deferment")
    again = CliRunner().invoke(app, ["golden", "label", "docs", "--questions", str(asked), "--path", str(db_path), "--out", str(out)])
    assert again.exit_code == 1 and "not written over" in flat(again)


POLICIES = {
    "deferment": (
        "Payment deferment lets customers in declared disaster areas put off their loan payments for up to six months. "
        "No late fees are charged while a payment is deferred, and interest keeps running at the usual rate."
    ),
    "emergency-fund": (
        "Emergency fund access gives displaced customers their savings the same day, without the usual notice period. "
        "The bank waives the fee for replacing lost cards and cheques while a customer is displaced."
    ),
}


class Writer:
    """The chat model the settings would name: it asks about the first sentence of what it is given, and passes all it judges."""

    label = "fake-writer"

    def __init__(self):
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        prompt = messages[-1]["content"]
        if prompt.startswith(("Score the passage", "A test question was written")):
            return json.dumps({"clarity": 1, "depth": 1, "structure": 1, "relevance": 1, "standalone": 1, "clear": 1, "answered": 1, "feedback": ""})
        passage = prompt.split('">\n', 1)[1].split("\n</passage>", 1)[0]
        sentence = passage.split(". ")[0]
        topic = "a deferred loan payment" if "deferment" in passage else "money for a displaced customer"
        return json.dumps({"question": f"What does the bank offer on {topic}?", "answer": sentence, "quotes": [sentence]})


@pytest.fixture
def writer(monkeypatch):
    from vectrixdb.evaluation import ChatWriter

    fake = Writer()
    monkeypatch.setattr(ChatWriter, "from_environment", classmethod(lambda cls, env=None, **kwargs: fake))
    return fake


def test_write_drafts_questions_from_the_chunks_and_never_writes_over_a_file(tmp_path, writer):
    from vectrixdb import Vectrix

    path = tmp_path / "policies"
    db = Vectrix("policies", path=str(path))
    for doc_id, text in POLICIES.items():
        db.add_document(text, doc_id=doc_id)
    db.close()
    out = tmp_path / "written.jsonl"
    result = CliRunner().invoke(app, ["golden", "write", "policies", "--path", str(path), "--out", str(out), "-n", "2", "--evolve", "0"])
    assert result.exit_code == 0, result.output
    assert "Wrote 2 of 2 questions" in flat(result) and 'delete "draft" from the ones you checked' in flat(result)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["expected"][0] for r in rows) == sorted(POLICIES) and all(r["draft"] is True for r in rows)
    assert (tmp_path / "written.jsonl.answers.jsonl").exists(), "the answers are kept beside the rows"
    again = CliRunner().invoke(app, ["golden", "write", "policies", "--path", str(path), "--out", str(out)])
    assert again.exit_code == 1 and "not written over" in flat(again)


def test_write_with_no_model_says_which_settings_name_one(tmp_path, db_path, monkeypatch):
    for name in ("AZURE_OPENAI_WRITER_DEPLOYMENT", "VECTRIXDB_WRITER_URL"):
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(app, ["golden", "write", "docs", "--path", str(db_path), "--out", str(tmp_path / "w.jsonl")])
    assert result.exit_code == 1 and "VECTRIXDB_WRITER_URL" in flat(result)
    assert not (tmp_path / "w.jsonl").exists()


MORE = {
    "divestment": "Early divestment guidance for customers who need to sell investments before maturity.",
    "credit": "Pre-authorized disaster credit: an emergency credit line increase for customers in declared zones.",
    "insurance": "Home insurance claims after a wildfire are filed with the insurer, and an adjuster visits the property.",
    "branches": "Branch opening hours during an evacuation are posted online, and mobile branches visit shelters.",
}
ASKED = {
    "deferment": "Can I postpone a loan payment with no late fees?",
    "emergency-fund": "How do displaced customers get expedited access to funds?",
    "divestment": "Should I sell investments before maturity?",
    "credit": "Can my emergency credit line be increased in a declared zone?",
    "insurance": "How are home insurance claims filed after a wildfire?",
    "branches": "What are branch opening hours during an evacuation?",
}
OUTSIDE = ["How do zebras sleep?", "Who won the 1966 World Cup?", "What is the boiling point of tungsten?", "How is sourdough bread made?", "Which planet has the most moons?"]


def test_cutoff_measures_where_to_stop_answering(tmp_path, db_path):
    from vectrixdb import Vectrix

    db = Vectrix("docs", path=str(db_path))
    for doc_id, text in MORE.items():
        db.add_document(text, doc_id=doc_id)
    db.close()
    golden = tmp_path / "golden.jsonl"
    golden.write_text("".join(json.dumps({"id": d, "question": q, "expected": [d]}) + "\n" for d, q in ASKED.items()), encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("\n".join(OUTSIDE) + "\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["golden", "cutoff", "docs", "--golden", str(golden), "--path", str(db_path), "--unanswerable", str(outside)])
    assert result.exit_code == 0, result.output
    assert "6 answers and 5 near misses" in flat(result) and "Cut-off 0." in flat(result) and "outscores a near miss 100%" in flat(result)

    few = CliRunner().invoke(app, ["golden", "cutoff", "docs", "--golden", str(golden), "--path", str(db_path)])
    assert few.exit_code == 1 and "too few near misses" in flat(few)


def test_sweep_builds_from_the_originals_ranks_and_changes_nothing(tmp_path, db_path):
    originals = tmp_path / "originals"
    originals.mkdir()
    for doc_id, text in TEXTS.items():
        (originals / f"{doc_id}.md").write_text(f"---\ndoc_id: {doc_id}\n---\n\n# {doc_id}\n\n{text}\n", encoding="utf-8")
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"id": "g1", "question": "Can I postpone a loan payment?", "expected": ["deferment"]}) + "\n", encoding="utf-8")
    out = tmp_path / "sweep.json"
    result = CliRunner().invoke(app, ["sweep", str(originals), "--golden", str(golden), "--size", "300", "--heading", "no", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "| 1 | " in flat(result) and "Nothing was changed" in flat(result)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["builds"] == 2 and {r["chunk"] for r in report["rows"]} == {"recursive", "markdown"} and report["best"]["recall"]["@1"] == 1.0
    bad = CliRunner().invoke(app, ["sweep", str(originals), "--golden", str(golden), "--heading", "maybe"])
    assert bad.exit_code == 1 and "yes, no or both" in flat(bad)
