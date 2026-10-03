"""The search-modes script: the page section it rewrites, and the sentences under its table."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare_modes.py"
spec = importlib.util.spec_from_file_location("compare_modes", SCRIPT)
compare_modes = importlib.util.module_from_spec(spec)
sys.modules["compare_modes"] = compare_modes
spec.loader.exec_module(compare_modes)

RESULTS = {
    "dense": {"mrr": 0.95, "recall1": 0.92, "ms": 7.0},
    "hybrid, rerank=False": {"mrr": 0.94, "recall1": 0.90, "ms": 20.0},
    "hybrid": {"mrr": 0.927, "recall1": 0.90, "ms": 211.0},
    "ultimate": {"mrr": 0.927, "recall1": 0.90, "ms": 882.0},
}


class TestTheSection:
    def test_the_sections_after_it_are_kept(self):
        """It cut the page at its own heading and dropped the rest, so a rerun
        deleted the Languages section written after it."""
        page = (
            "# Search modes\n\nIntro.\n\n## Which mode earns its cost\n\nold table\n\n"
            "## Languages\n\nBM25 has no model.\n\n## More than one dense model\n\nText.\n"
        )
        out = compare_modes.replace_section(page, compare_modes.render(RESULTS))
        assert "old table" not in out
        assert out.count("## Which mode earns its cost") == 1
        assert "## Languages\n\nBM25 has no model." in out
        assert out.index("## Which mode earns its cost") < out.index("## Languages")
        assert out.rstrip().endswith("Text.")

    def test_a_page_without_it_gets_it_at_the_end(self):
        out = compare_modes.replace_section("# Search modes\n", compare_modes.render(RESULTS))
        assert out.startswith("# Search modes\n")
        assert out.rstrip().endswith("least likely to contain.")

    def test_the_real_page_keeps_every_heading(self):
        page = (SCRIPT.parents[1] / "docs" / "explanation" / "search-modes.md").read_text(
            encoding="utf-8"
        )
        out = compare_modes.replace_section(page, compare_modes.render(RESULTS))

        def headings(text):
            return [line for line in text.splitlines() if line.startswith("## ")]

        assert headings(out) == headings(page)


class TestTheReading:
    def test_every_row_has_its_own_line_and_the_reranker_is_shown_apart(self):
        table = compare_modes.render(RESULTS)
        for label in compare_modes.MODES:
            assert f"| {label} |" in table
        assert "hybrid, rerank=False" in compare_modes.MODES

    def test_ratios_and_the_reranker_effect_come_from_the_numbers(self):
        text = " ".join(compare_modes._reading(RESULTS))
        assert "dense ranks best and costs least" in text
        assert "hybrid without the reranker costs 2.9 times what dense does" in text
        assert "hybrid 30 times and ultimate 126 times" in text
        assert "lowers MRR by 0.013" in text and "91% of hybrid's time" in text

    def test_a_reranker_that_helps_is_said_to(self):
        better = {**RESULTS, "hybrid": {"mrr": 0.97, "recall1": 0.95, "ms": 211.0}}
        text = " ".join(compare_modes._reading(better))
        assert "raises MRR by 0.030" in text
        assert "hybrid ranks best and dense costs least" in text
