"""compare_extractors: several readers over your own pages, side by side.

It runs the readers and measures; it does not judge which text is right. What
is held to: every reader runs over every file, a reader that fails on a file
is a finding and not the end of the run, the quality score is the library's
own, and the files where readers disagree come first.
"""

from __future__ import annotations

import pytest

from vectrixdb.extract.compare import Comparison, compare_extractors

GOOD = (
    "Customers in a declared disaster area can defer loan payments for up to ninety days with no late fees. "
    "Interest still accrues during the deferral, and an adviser calls within a day to confirm the new schedule. "
    "Displaced customers get expedited access to their funds and waived withdrawal fees for thirty days. "
    "Anyone who needs to sell investments before maturity is shown the penalty before they confirm the sale."
)
# The same words, read across two columns instead of down them.
_words = GOOD.split()
COLUMNS_MIXED = " ".join(w for pair in zip(_words[: len(_words) // 2], _words[len(_words) // 2 :]) for w in pair)
NOISE = "tbe qnick brovvn f0x jurnps ovcr tbe 1azy d0g xq zzkj wplm " * 8


def reader(text):
    return lambda data, name: text


def failing(data, name):
    raise RuntimeError("the service answered 502")


FILES = [("memo.pdf", b"%PDF one"), ("scan.pdf", b"%PDF two")]


class TestEveryReaderOverEveryFile:
    def test_one_reading_for_each_pair(self):
        found = compare_extractors(FILES, {"a": reader(GOOD), "b": reader(GOOD)})
        assert [(r.file, r.reader) for r in found.readings] == [("memo.pdf", "a"), ("memo.pdf", "b"), ("scan.pdf", "a"), ("scan.pdf", "b")]

    def test_each_is_measured_with_the_librarys_own_quality_score(self):
        found = compare_extractors(FILES[:1], {"clean": reader(GOOD), "noise": reader(NOISE)})
        clean, noise = found.of("memo.pdf", "clean"), found.of("memo.pdf", "noise")
        assert clean.usable and clean.quality > 0.9 and clean.characters == len(GOOD)
        assert not noise.usable and noise.quality < clean.quality

    def test_a_reader_that_fails_is_a_finding_and_the_rest_still_run(self):
        found = compare_extractors(FILES, {"down": failing, "up": reader(GOOD)})
        down = found.of("memo.pdf", "down")
        assert down.error == "RuntimeError: the service answered 502" and down.characters == 0
        assert found.of("scan.pdf", "up").usable
        assert not any("down" in key for key in found.alike), "a reader with no text is not compared with anything"

    def test_none_is_the_built_in_reader(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("# Late fees\n\n" + GOOD, encoding="utf-8")
        found = compare_extractors([note], {"built in": None, "ours": reader(GOOD)})
        assert found.of("note.md", "built in").usable and found.of("note.md", "built in").error is None

    def test_a_structured_reply_is_read_as_the_registry_reads_it(self):
        reply = lambda data, name: {"text": GOOD, "pages": [[0, 1], [50, 2]], "metadata": {"pages_ocr": 2}}  # noqa: E731
        got = compare_extractors(FILES[:1], {"service": reply}).of("memo.pdf", "service")
        assert got.pages == 2 and got.pages_ocr == 2

    def test_no_readers_is_said_so(self):
        with pytest.raises(ValueError, match="at least one reader"):
            compare_extractors(FILES, {})


class TestWhereTheyDisagree:
    def test_readers_that_agree_are_alike_and_ones_that_do_not_are_not(self):
        found = compare_extractors(FILES[:1], {"a": reader(GOOD), "b": reader(GOOD), "scrambled": reader(COLUMNS_MIXED)})
        assert found.alike[("memo.pdf", "a", "b")] == 1.0
        assert found.alike[("memo.pdf", "a", "scrambled")] < 0.9

    def test_the_least_alike_come_first(self):
        found = compare_extractors(FILES[:1], {"a": reader(GOOD), "scrambled": reader(COLUMNS_MIXED), "noise": reader(NOISE)})
        apart = found.disagreements()
        assert [row[3] for row in apart] == sorted(row[3] for row in apart) and apart[0][3] < apart[-1][3]
        assert all(row[0] == "memo.pdf" for row in apart)

    def test_two_readers_that_agree_are_not_listed(self):
        assert compare_extractors(FILES, {"a": reader(GOOD), "b": reader(GOOD)}).disagreements() == []

    def test_scrambled_columns_pass_the_quality_score_which_is_why_the_comparison_exists(self):
        """Every word is a good word, so the score cannot see it. Only another reader can."""
        found = compare_extractors(FILES[:1], {"right": reader(GOOD), "scrambled": reader(COLUMNS_MIXED)})
        assert found.of("memo.pdf", "scrambled").usable and found.disagreements()


class TestWhatAPersonReads:
    def test_the_table_and_the_disagreements(self):
        found = compare_extractors(FILES[:1], {"a": reader(GOOD), "scrambled": reader(COLUMNS_MIXED), "down": failing})
        said = found.to_markdown()
        assert "| File | Reader | Quality |" in said and "| memo.pdf | a |" in said
        assert "failed: RuntimeError: the service answered 502" in said
        assert "Where the readers disagree" in said and "memo.pdf: a and scrambled are" in said

    def test_the_dict_leaves_the_texts_out(self):
        found = compare_extractors(FILES[:1], {"a": reader(GOOD)})
        assert isinstance(found, Comparison) and "text" not in found.to_dict()["readings"][0]
