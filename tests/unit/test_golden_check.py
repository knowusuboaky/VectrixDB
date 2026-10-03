"""A golden file is held to one schema before a run, and every mistake in it is said at once.

A run searches every question every way a collection can be searched, which
takes minutes and calls the service. A file with a mistake in it started one
anyway, because the reader skipped what it did not know: a row with
``expeted`` in it dropped out of the scores without a word, and a file with
five mistakes took five uploads to find them. The schema is the contract,
published as docs/reference/golden.schema.json, and check_golden reads its
rules off the same dict, so the two cannot disagree.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import numpy as np
import pytest

from vectrixdb.evaluation import (
    GOLDEN_SCHEMA,
    MissingDocumentsWarning,
    Question,
    check_golden,
    golden_template,
    read_golden,
    save_questions,
)

ROOT = Path(__file__).resolve().parents[2]
GOOD = {"id": "g1", "question": "What was total revenue?", "expected": ["td/report.pdf"]}


def written(tmp_path, *rows, name="golden.jsonl"):
    path = tmp_path / name
    path.write_text(
        "".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def said(check):
    return [(p.line, p.message) for p in check.errors]


def small_collection(tmp_path):
    from vectrixdb import Vectrix

    def embed(texts):
        return np.asarray([[1.0, float(len(t) % 7), 0.5] for t in texts], dtype=np.float32)

    db = Vectrix(
        "golden", path=str(tmp_path / "db"), embed_fn=embed, dimension=3, embedding_cache=False
    )
    db.add_document(
        "# Deferment\n\nA payment can be deferred for up to three months.", doc_id="deferment"
    )
    db.add_document("# Fees\n\nA late fee is charged after ten days.", doc_id="fees")
    return db


class TestTheSchema:
    def test_the_published_file_is_the_schema_the_check_uses(self):
        published = json.loads(
            (ROOT / "docs" / "reference" / "golden.schema.json").read_text(encoding="utf-8")
        )
        assert published == GOLDEN_SCHEMA

    def test_a_question_and_where_its_answer_is_are_required_and_nothing_else_is_allowed(self):
        assert GOLDEN_SCHEMA["required"] == ["question", "expected"]
        assert GOLDEN_SCHEMA["additionalProperties"] is False
        assert set(GOLDEN_SCHEMA["properties"]) == {
            "id",
            "question",
            "expected",
            "reference",
            "evidence",
            "hint",
            "draft",
        }

    def test_an_empty_question_is_a_template_row_only_with_a_hint(self):
        assert GOLDEN_SCHEMA["then"] == {"required": ["hint"]}
        assert GOLDEN_SCHEMA["if"]["properties"]["question"]["pattern"] == "^\\s*$"

    @pytest.mark.parametrize(
        "entry, fine",
        [
            ("td/report.pdf", True),
            ("td/report.pdf#page=41", True),
            ("td/report.pdf#page=0", False),
            ("td/report.pdf#page=x", False),
            ("#page=4", False),
            ("a#page=4#page=5", False),
        ],
    )
    def test_an_expected_entry_is_a_document_or_one_page_of_it(self, entry, fine):
        assert (
            bool(re.search(GOLDEN_SCHEMA["properties"]["expected"]["items"]["pattern"], entry))
            is fine
        )


class TestEveryMistakeAtOnce:
    def test_a_file_with_eight_mistakes_is_told_all_eight(self, tmp_path):
        path = written(
            tmp_path,
            GOOD,
            {"id": "g2", "question": "", "expected": ["td/report.pdf"], "hint": "Who we are"},
            {
                "id": "g3",
                "question": "How much was set aside?",
                "expected": ["td/report.pdf"],
                "draft": True,
            },
            {
                "id": "g4",
                "question": "What is the CET1 ratio?",
                "expeted": ["td/report.pdf#page=77"],
            },
            {"id": "g5", "question": "Who leads the bank?", "expected": "td/report.pdf"},
            {"id": "g6", "question": "How many employees?", "expected": []},
            {"id": "g3", "question": "What is the dividend?", "expected": ["td/report.pdf#page=0"]},
            {"text": "An old spelling", "expected": ["td/report.pdf"]},
            {"id": "g9", "question": "what was TOTAL   revenue?", "expected": ["td/report.pdf"]},
            "not json at all",
        )
        check = check_golden(path)
        assert not check.ok and check.golden is None
        assert said(check) == [
            (4, '"expeted" is not a field. Did you mean "expected"?'),
            (5, 'expected should be a list, not text, like ["td/report.pdf"]'),
            (6, "expected is empty: name the document that answers it"),
            (
                7,
                "td/report.pdf#page=0 should end in a page number from 1, like #page=41, or name the document alone",
            ),
            (7, "id g3 is used twice, first on line 3"),
            (8, 'text is the old name for question: rename it "question"'),
            (9, "the same question as line 1"),
            (10, "is not JSON: expecting value at character 1"),
        ]
        assert (check.rows, check.ready, check.unfilled, check.drafts) == (10, 2, 1, 1)

    def test_the_summary_reads_as_a_list(self, tmp_path):
        path = written(
            tmp_path,
            GOOD,
            {"id": "g1", "question": "Another?", "expected": ["x"]},
            "{",
            name="golden-financial.jsonl",
        )
        assert check_golden(path).summary().splitlines() == [
            "golden-financial.jsonl: 2 problems",
            "  line 2  id g1 is used twice, first on line 1",
            "  line 3  is not JSON: expecting property name enclosed in double quotes at character 2",
        ]


class TestEachRuleInWords:
    @pytest.mark.parametrize(
        "row, message",
        [
            ({"question": "Q?"}, "expected is missing: name the document that answers it"),
            ({"expected": ["a"]}, "question is missing"),
            (
                {"question": "Q?", "expected": ["a"], "draft": "yes"},
                "draft should be true or false, not text",
            ),
            (
                {"question": "Q?", "expected": ["a"], "id": 7},
                "id should be text or null, not a number",
            ),
            ({"question": "Q?", "expected": ["a"], "id": ""}, "id is empty"),
            ({"question": "Q?", "expected": ["a", "a"]}, "expected names a twice"),
            ({"question": "Q?", "expected": [""]}, "expected has an empty id in it"),
            (
                {"question": "Q?", "expected": [3]},
                "expected should hold document ids as text, not a number",
            ),
            ({"question": "  ", "expected": ["a"]}, "question is empty"),
            (
                {"question": "Q?", "expected": ["a"], "reference": 4},
                "reference should be text, not a number",
            ),
            (
                {"question": "Q?", "expected": ["a"], "notes": "x"},
                '"notes" is not a field. The fields are id, question, expected, reference, evidence, hint and draft',
            ),
            (["a list"], "is not a JSON object: each line is one question, in braces"),
        ],
    )
    def test_it_is_said(self, tmp_path, row, message):
        check = check_golden(written(tmp_path, GOOD, row))
        assert [m for _line, m in said(check)] == [message]


class TestNoFileYet:
    """A fresh deployment, live on 2026-10-02: the reply to a run was a page of Blob storage XML."""

    def test_no_file_is_missing_with_one_problem_that_says_what_to_do(self, tmp_path):
        check = check_golden(tmp_path / "golden.jsonl")
        assert not check.ok and check.missing and len(check.errors) == 1
        assert "there are no golden questions at" in str(check.errors[0])
        assert "vectrixdb golden write" in str(check.errors[0])

    def test_a_store_that_says_not_found_is_missing_too(self):
        class ResourceNotFoundError(Exception):
            pass

        class Blob:
            def fetch(self, uri):
                raise ResourceNotFoundError(
                    "The specified blob does not exist. ErrorCode:BlobNotFound"
                )

        check = check_golden(
            "https://acct.blob.core.windows.net/evals/golden.jsonl", fetcher=Blob()
        )
        assert check.missing and "BlobNotFound" not in str(check.errors[0])

    def test_any_other_failure_is_raised(self):
        class Refused:
            def fetch(self, uri):
                raise PermissionError("AuthorizationPermissionMismatch")

        with pytest.raises(PermissionError):
            check_golden("https://acct.blob.core.windows.net/evals/golden.jsonl", fetcher=Refused())


class TestWhatIsFine:
    def test_a_template_row_waits_rather_than_fails(self, tmp_path):
        check = check_golden(
            written(
                tmp_path,
                GOOD,
                {"id": "g2", "question": "", "expected": ["a"], "hint": "The start of it"},
            )
        )
        assert check.ok and (check.ready, check.unfilled) == (1, 1)
        assert [p.message for p in check.warnings] == [
            "1 template row is still waiting for a question"
        ]

    def test_nothing_but_template_rows_is_not_ready(self, tmp_path):
        check = check_golden(
            written(tmp_path, {"id": "g1", "question": "", "expected": ["a"], "hint": "h"})
        )
        assert said(check) == [
            (
                None,
                "no question is ready yet: 1 template row is waiting for one. Fill some in and try again",
            )
        ]

    def test_an_empty_file(self, tmp_path):
        check = check_golden(written(tmp_path, "   "))
        assert said(check) == [(None, "the file is empty: one question a line, each a JSON object")]

    def test_a_bedrock_dataset_names_no_documents(self, tmp_path):
        turn = {
            "conversationTurns": [
                {"prompt": {"content": [{"text": "Can I defer?"}]}, "referenceResponses": []}
            ]
        }
        check = check_golden(written(tmp_path, turn))
        assert not check.ok and "Label it first: vectrixdb golden label" in check.errors[0].message

    def test_drafts_are_a_note_not_a_stop(self, tmp_path):
        check = check_golden(
            written(
                tmp_path,
                {**GOOD, "draft": True},
                {"id": "g2", "question": "Q2?", "expected": ["a"], "draft": True},
            )
        )
        assert check.ok and check.drafts == 2
        assert [p.message for p in check.warnings] == [
            "drafts nobody has checked, on lines 1 and 2: read each against its document, then delete draft"
        ]

    def test_a_good_file_comes_back_read_the_way_read_golden_reads_it(self, tmp_path):
        path = written(
            tmp_path,
            GOOD,
            {
                "id": "g2",
                "question": "On a page?",
                "expected": ["td/report.pdf#page=41"],
                "reference": "Yes.",
            },
        )
        check = check_golden(path)
        assert check.ok and check.golden == read_golden(path)
        assert check.to_dict()["ok"] is True and check.to_dict()["errors"] == []


class TestWhatTheLibraryWritesPasses:
    def test_a_template_and_a_template_filled_in(self, tmp_path):
        db = small_collection(tmp_path)
        try:
            rows = golden_template(db, tmp_path / "template.jsonl", n=2)
        finally:
            db.close()
        blank = check_golden(tmp_path / "template.jsonl")
        assert [m for _l, m in said(blank)] == [
            "no question is ready yet: 2 template rows are waiting for one. Fill some in and try again"
        ]
        rows[0]["question"] = "How long can a payment be deferred?"
        filled = written(tmp_path, *rows, name="filled.jsonl")
        assert check_golden(filled).ok

    def test_questions_saved_by_the_library_even_without_an_id(self, tmp_path):
        save_questions(
            [Question("Q?", "A.", ["a"], "q1"), Question("R?", expected=["b"])],
            tmp_path / "saved.jsonl",
        )
        assert check_golden(tmp_path / "saved.jsonl").ok

    def test_what_a_writer_drafted_is_a_note(self, tmp_path):
        db = small_collection(tmp_path)
        try:
            golden_template(
                db,
                tmp_path / "drafted.jsonl",
                n=2,
                writer=lambda text: "What does this say?" + str(len(text)),
            )
        finally:
            db.close()
        check = check_golden(tmp_path / "drafted.jsonl")
        assert check.ok and check.drafts == 2


class TestAgainstTheCollection:
    def test_over_several_collections_a_document_is_missing_only_when_none_holds_it(self, tmp_path):
        """One golden file over two collections: each lacks the other's documents, and that is no loss."""
        from vectrixdb import Vectrix

        def embed(texts):
            return np.asarray([[1.0, float(len(t) % 7), 0.5] for t in texts], dtype=np.float32)

        first = small_collection(tmp_path)
        second = Vectrix(
            "media",
            path=str(tmp_path / "media"),
            embed_fn=embed,
            dimension=3,
            embedding_cache=False,
        )
        second.add_document("# Clip\n\nThe clip shows a rocket launch at dawn.", doc_id="clip.mp4")
        path = written(
            tmp_path,
            {"id": "q1", "question": "Can I defer a payment?", "expected": ["deferment"]},
            {"id": "q2", "question": "What does the clip show?", "expected": ["clip.mp4"]},
            {"id": "q3", "question": "What about travel?", "expected": ["travel-memo"]},
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MissingDocumentsWarning)
                check = check_golden(path, [first, second])
        finally:
            first.close()
            second.close()
        assert check.ok
        notes = [w.message for w in check.warnings if "has no document" in w.message]
        assert len(notes) == 1 and notes[0].startswith(
            "every collection has no document called travel-memo"
        ), notes
        assert "q3" in notes[0] and "q1" not in notes[0] and "q2" not in notes[0]

    def test_evidence_no_chunk_holds_any_more_is_a_note(self, tmp_path):
        """The quotes were written from an earlier reading of the document; a quote still on its page is not noted."""
        db = small_collection(tmp_path)
        path = written(
            tmp_path,
            {
                "id": "q1",
                "question": "Can I defer a payment?",
                "expected": ["deferment"],
                "evidence": ["deferred for up to six months with no late fees at all"],
            },
            {
                "id": "q2",
                "question": "When is a late fee charged?",
                "expected": ["fees"],
                "evidence": ["charged after ten days"],
            },
            {
                "id": "q3",
                "question": "What about travel?",
                "expected": ["travel-memo"],
                "evidence": ["abroad for a month"],
            },
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MissingDocumentsWarning)
                check = check_golden(path, db)
        finally:
            db.close()
        assert check.ok
        stale = [w.message for w in check.warnings if "no longer holds the evidence" in w.message]
        assert len(stale) == 1 and "1 question (q1)" in stale[0], stale
        assert "q3" not in stale[0], (
            "a document not held is the missing-documents note's, not this one's"
        )

    def test_a_document_the_collection_does_not_hold_is_a_note_and_not_said_twice(self, tmp_path):
        db = small_collection(tmp_path)
        path = written(
            tmp_path,
            {"id": "q1", "question": "Can I defer a payment?", "expected": ["deferment"]},
            {"id": "q2", "question": "What about travel?", "expected": ["travel-memo"]},
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", MissingDocumentsWarning)
                check = check_golden(path, db)
        finally:
            db.close()
        assert check.ok
        assert len(check.warnings) == 1 and "travel-memo" in check.warnings[0].message
