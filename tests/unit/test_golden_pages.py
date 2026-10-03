"""A golden answer can be one page of a document, and a template can ask a page at a time.

Found planning the Azure walkthrough's evaluation: its financial collection
holds one document, a bank's annual report of 244 pages. Scored by document,
every setup finds "the right document" for every question about it, every
setup ties, and the three picks come down to speed alone. So an expected
entry may name a page, ``report.pdf#page=41``, the page's place in the file as
its citation gives it, and a result counts when its chunk covers that page.
"""

from __future__ import annotations

import types
import warnings
import zlib

import numpy as np
import pytest

from vectrixdb.evaluation import (
    MissingDocumentsWarning,
    Question,
    answer_cutoff,
    check_golden,
    golden_template,
    missing_documents,
    retrieval_report,
    run_setup,
)
from vectrixdb.evaluation import _answers
from vectrixdb.ingest import LoadedDocument, join_pages

WIDTH = 64


def embed(texts):
    """Words hashed into a vector, so a question finds the page that shares its words."""
    out = np.zeros((len(texts), WIDTH), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().replace(",", " ").replace(".", " ").replace("?", " ").split():
            out[i, zlib.crc32(word.encode()) % WIDTH] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


PAGES = [
    "Total revenue rose to fifty billion dollars this year as lending and wealth revenue grew in every region. "
    "Retail revenue led the gains across the country, and revenue from the markets business rose as well.",
    "The provision for credit losses rose as the bank set aside more for impaired loans this year. "
    "Credit card losses and commercial credit exposures both grew in a slower economy, so more money was set aside "
    "than the year before.",
    "Dividends declared to common shareholders increased, and the board raised the quarterly dividend twice. "
    "The bank also bought back common shares under its normal course issuer bid through the whole year.",
    "Photo credits.",
]
ASKED = "How much was set aside for credit losses?"


def report():
    text, pages = join_pages(PAGES)
    credit = pages[1][0]
    return LoadedDocument(
        text=text,
        pages=pages,
        headings=[(credit, "Credit risk", 2)],
        page_labels={1: "i", 2: "ii", 3: "1", 4: "2"},
    )


@pytest.fixture
def db(tmp_path):
    from vectrixdb import Vectrix
    from vectrixdb.extract.engines import segments_to_document

    handle = Vectrix(
        "annual",
        path=str(tmp_path / "db"),
        embed_fn=embed,
        dimension=WIDTH,
        mode="dense",
        keep_source=str(tmp_path / "kept"),
        embedding_cache=False,
    )
    handle.add_document(report(), doc_id="report.pdf", chunk="markdown", chunk_size=280, overlap=0)
    handle.add_document(
        "# Notes\n\nThe figures in these notes are unaudited and rounded to the nearest million dollars.",
        doc_id="notes.md",
    )
    # Parts long enough to be pages, so it is being a recording that keeps it to one row.
    said = (
        "and every region reported growth in lending, deposits and wealth, with the bank's revenue up on the year. "
        * 2
    )
    handle.add_document(
        segments_to_document(
            [(0.0, 50.0, "Welcome to the call, " + said), (65.2, 120.0, "Revenue is up, " + said)]
        ),
        doc_id="call.wav",
    )
    # One page is the document itself.
    handle.add_document(
        LoadedDocument(text=PAGES[0] + " " + PAGES[2], pages=[(0, 1)]), doc_id="memo.pdf"
    )
    yield handle
    handle.close()


def hit(doc, page=None, page_end=None, chunk=0):
    meta = {"_vx_doc": doc}
    if page is not None:
        meta["page"] = page
    if page_end is not None:
        meta["page_end"] = page_end
    return types.SimpleNamespace(id=f"{doc}:{chunk}", metadata=meta)


class TestAPageIsAnAnswer:
    def test_a_chunk_answers_the_pages_it_covers_and_its_document(self):
        spanning = hit("report.pdf", 2, 3)
        assert _answers(spanning, ["report.pdf#page=3"], "doc")
        assert _answers(spanning, ["report.pdf#page=2"], "doc")
        assert not _answers(spanning, ["report.pdf#page=4"], "doc")
        assert _answers(spanning, ["report.pdf"], "doc")
        assert not _answers(spanning, ["other.pdf#page=2"], "doc")

    def test_a_chunk_with_no_pages_answers_its_document_and_no_page_of_it(self):
        assert _answers(hit("notes.md"), ["notes.md"], "doc")
        assert not _answers(hit("notes.md"), ["notes.md#page=1"], "doc")

    def test_by_chunk_still_means_the_chunk(self):
        assert _answers(hit("report.pdf", 2, chunk=5), ["report.pdf:5"], "chunk")
        assert not _answers(hit("report.pdf", 2, chunk=5), ["report.pdf#page=2"], "chunk")


class TestScoredByPage:
    def test_the_document_is_always_found_and_the_page_is_what_tells(self, db):
        """The whole reason: by document, the wrong page would score as perfectly as the right one."""
        right = retrieval_report(
            db, [Question(ASKED, expected=["report.pdf#page=2"], id="p2")], k=(1,)
        )
        wrong = retrieval_report(
            db, [Question(ASKED, expected=["report.pdf#page=3"], id="p3")], k=(1,)
        )
        whole = retrieval_report(db, [Question(ASKED, expected=["report.pdf"], id="doc")], k=(1,))
        assert (right["recall"]["@1"], wrong["recall"]["@1"], whole["recall"]["@1"]) == (
            1.0,
            0.0,
            1.0,
        )
        assert wrong["misses"] == [] or wrong["misses"][0]["got"][0].startswith("report.pdf#page=2")

    def test_results_are_ranked_by_page_so_a_later_page_of_the_same_document_is_found(self, db):
        """Ranked by document, the report would take one place, held by its first chunk, and page 3 would never be reached."""
        further = retrieval_report(
            db, [Question(ASKED, expected=["report.pdf#page=3"], id="p3")], k=(10,)
        )
        assert further["recall"]["@10"] == 1.0 and further["misses"] == []

    def test_a_run_ranks_the_first_chunk_on_the_page(self, db):
        questions = [
            Question(ASKED, expected=["report.pdf#page=2"], id="p2"),
            Question(ASKED, expected=["report.pdf#page=3"], id="p3"),
        ]
        ranks = run_setup(db, {"key": "dense", "search": {"mode": "dense"}}, questions)["ranks"]
        assert ranks[0] == 1 and (ranks[1] is None or ranks[1] > 1)

    def test_the_answer_cutoff_counts_the_wrong_page_as_a_near_miss(self, db):
        found = answer_cutoff(
            db,
            [
                Question(ASKED, expected=["report.pdf#page=2"], id="p2"),
                Question(ASKED, expected=["report.pdf#page=3"], id="p3"),
            ],
        )
        assert (found["answers"], found["near_misses"]) == (1, 1)

    def test_a_page_is_looked_up_by_its_document(self, db):
        asked = Question(ASKED, expected=["report.pdf#page=2", "gone.pdf#page=1"], id="m")
        lost = Question(ASKED, expected=["gone.pdf#page=1"], id="m2")
        with pytest.warns(MissingDocumentsWarning):
            gone = missing_documents(db, [asked, lost])
        assert gone == {"VectrixDB": {"documents": ["gone.pdf"], "questions": ["m2"]}}
        with warnings.catch_warnings():
            warnings.simplefilter("error", MissingDocumentsWarning)
            assert (
                missing_documents(db, [Question(ASKED, expected=["report.pdf#page=2"], id="ok")])
                == {}
            )


class TestATemplateAPageAtATime:
    def test_a_row_a_page_with_text_and_a_row_a_document_without_pages(self, db):
        rows = golden_template(db, n=50, by="page")
        assert [r["expected"] for r in rows] == [
            ["call.wav"],
            ["memo.pdf"],
            ["notes.md"],
            ["report.pdf#page=1"],
            ["report.pdf#page=2"],
            ["report.pdf#page=3"],
        ]
        assert all(r["question"] == "" for r in rows), "a template is for a person to write in"

    def test_the_hint_says_which_page_its_printed_number_and_its_heading(self, db):
        second = next(
            r
            for r in golden_template(db, n=50, by="page")
            if r["expected"] == ["report.pdf#page=2"]
        )
        assert second["hint"].startswith(
            "Page 2, printed ii, under Credit risk: The provision for credit losses"
        )

    def test_a_long_report_is_asked_about_all_the_way_through(self, tmp_path):
        from vectrixdb import Vectrix

        pages = [
            f"Section {n} of the report covers topic number {n} in depth, " * 5
            for n in range(1, 31)
        ]
        text, offsets = join_pages(pages)
        handle = Vectrix(
            "long",
            path=str(tmp_path / "long"),
            embed_fn=embed,
            dimension=WIDTH,
            mode="dense",
            keep_source=str(tmp_path / "kept-long"),
            embedding_cache=False,
        )
        try:
            handle.add_document(LoadedDocument(text=text, pages=offsets), doc_id="long.pdf")
            first = golden_template(handle, n=5, by="page", seed=0)
            again = golden_template(handle, n=5, by="page", seed=0)
        finally:
            handle.close()
        numbers = [int(r["expected"][0].split("=")[1]) for r in first]
        assert first == again and len(numbers) == 5
        assert [b - a for a, b in zip(numbers, numbers[1:])] == [6, 6, 6, 6], (
            "evenly spread, one every six pages of thirty"
        )

    def test_filled_in_it_passes_the_check(self, db, tmp_path):
        rows = golden_template(db, tmp_path / "golden.jsonl", n=50, by="page")
        rows[4]["question"] = ASKED
        (tmp_path / "golden.jsonl").write_text(
            "".join(__import__("json").dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        check = check_golden(tmp_path / "golden.jsonl", db)
        assert (
            check.ok
            and check.ready == 1
            and check.warnings[0].message == "5 template rows are still waiting for a question"
        )

    def test_by_is_doc_or_page(self, db):
        with pytest.raises(ValueError, match="by is 'doc' or 'page'"):
            golden_template(db, by="chunk")

    def test_several_collections_make_one_golden_file(self, db, tmp_path):
        """A golden dataset is not one a collection: its rows name documents wherever they are held."""
        from vectrixdb import Vectrix

        other = Vectrix(
            "media",
            path=str(tmp_path / "media"),
            embed_fn=embed,
            dimension=WIDTH,
            mode="dense",
            keep_source=str(tmp_path / "kept-media"),
            embedding_cache=False,
        )
        try:
            other.add_document(
                "# Talk\n\nA talk about basalt and how it forms when lava cools quickly at the surface.",
                doc_id="talk.md",
            )
            rows = golden_template([db, other], n=50, by="page")
        finally:
            other.close()
        assert [r["expected"][0] for r in rows] == [
            "call.wav",
            "memo.pdf",
            "notes.md",
            "report.pdf#page=1",
            "report.pdf#page=2",
            "report.pdf#page=3",
            "talk.md",
        ]
